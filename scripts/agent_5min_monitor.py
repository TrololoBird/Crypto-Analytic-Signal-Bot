"""5-minute signal & health monitor for SignalBot — runs via Cowork scheduled task.

Usage (standalone):
    python -m scripts.agent_5min_monitor [--lookback 300] [--config config.toml]

Output:
    Prints structured report to stdout.
    Appends JSON snapshot to logs/monitor_5min.jsonl
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta

UTC = UTC
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
LOG_DIR = ROOT / "data" / "bot" / "logs"
LIVE_WATCH = ROOT / "data" / "live_watch"
MONITOR_OUT = ROOT / "logs" / "monitor_5min.jsonl"

# ── Regex patterns ──────────────────────────────────────────────────────────

RE_FILTERED = re.compile(
    r"signal filtered\s+\|.*?setup=(\S+).*?dir=(\S+).*?score=([\d.]+).*?reason=(\S+)"
)
RE_SIGNAL_DIAG = re.compile(
    r"signal diagnostics\s+\|.*?detector_runs=(\d+).*?hits=(\d+).*?candidates=(\d+)"
    r".*?hit_rate=([\d.]+).*?top_filter_rejects=(\[.*?\])"
)
RE_DELIVERED = re.compile(r"deliver.*?signal_id=(\S+).*?setup=(\S+)", re.IGNORECASE)
RE_DEGRADED = re.compile(r"enrichment degraded\s+\|.*?symbol=(\S+).*?reason=([^\|]+)")
RE_RATE_LIMIT = re.compile(r"rate_limit_paused_(\d+)s")
RE_SL_PENALTY = re.compile(
    r"strategy sl-rate penalty\s+\|.*?setup=(\S+).*?sl_rate=([\d.]+).*?score=([\d.→.]+)"
)
RE_BENCHMARK_STALE = re.compile(
    r"benchmark context stale\s+\|.*?symbol=(\S+).*?age_seconds=([\d.]+)"
)
RE_ERROR = re.compile(r"\|\s+ERROR\s+\|(.+)")
RE_WARNING = re.compile(r"\|\s+WARNING\s+\|(.+)")
RE_WS_EVENT = re.compile(
    r"(ws_disconnect|ws_reconnect|ws.*?error|websocket.*?closed)", re.IGNORECASE
)
RE_CYCLE = re.compile(
    r"cycle\s+\|.*?symbol=(\S+).*?candidates=(\d+).*?delivered=(\d+).*?rejected=(\d+).*?status=(\S+)"
)
RE_EXCEPTION = re.compile(r"(Traceback|Exception|Error:)\s+", re.IGNORECASE)
RE_HARD_CONFLUENCE = re.compile(r"hard_confluence.*?score=(\d+)/(\d+)")
RE_SHORTLIST = re.compile(r"shortlist.*?symbols=(\d+)", re.IGNORECASE)


# ── Helpers ─────────────────────────────────────────────────────────────────


def _latest_bot_log() -> Path | None:
    logs = sorted(LOG_DIR.glob("bot_*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
    return logs[0] if logs else None


def _latest_live_watch() -> Path | None:
    dirs = (
        sorted(LIVE_WATCH.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)
        if LIVE_WATCH.exists()
        else []
    )
    return dirs[0] if dirs else None


def _tail_bytes(path: Path, nbytes: int = 600_000) -> list[str]:
    """Read the last `nbytes` of a file efficiently. Avoids TZ-mismatch issues."""
    if not path or not path.exists():
        return []
    size = path.stat().st_size
    offset = max(0, size - nbytes)
    with path.open("rb") as fh:
        fh.seek(offset)
        raw = fh.read()
    text = raw.decode("utf-8", errors="replace")
    lines = text.splitlines()
    # Drop first (potentially partial) line if we didn't start from beginning
    if offset > 0 and lines:
        lines = lines[1:]
    return lines


def _read_recent_lines(path: Path, since: datetime) -> list[str]:
    """Return recent lines from a bot log file.

    Bot logs use the local machine timezone (not necessarily matching the
    process timezone). To avoid cross-TZ issues we use a tail-by-bytes
    strategy and accept that we may include slightly more than `lookback`.
    """
    return _tail_bytes(path, nbytes=800_000)


def _parse_filter_rejects_json(raw: str) -> list[dict[str, Any]]:
    try:
        # convert Python repr to JSON: single quotes → double
        cleaned = raw.replace("'", '"')
        return json.loads(cleaned)
    except Exception:
        return []


def _latest_snapshots(watch_dir: Path) -> list[dict[str, Any]]:
    snap_file = watch_dir / "snapshots.jsonl"
    if not snap_file.exists():
        return []
    rows = []
    with snap_file.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                try:
                    rows.append(json.loads(line))
                except Exception:
                    pass
    return rows


# ── Analysis ────────────────────────────────────────────────────────────────


def analyze(lookback_seconds: int = 300) -> dict[str, Any]:
    # Bot logs use naive local timestamps — compare with local time
    since = datetime.now() - timedelta(seconds=lookback_seconds)
    report: dict[str, Any] = {
        "generated_at": datetime.now().isoformat(),
        "lookback_seconds": lookback_seconds,
        "window_start": since.isoformat(),
    }

    # ── Bot log analysis ────────────────────────────────────────────────────
    bot_log = _latest_bot_log()
    lines = _read_recent_lines(bot_log, since) if bot_log else []
    report["bot_log"] = str(bot_log) if bot_log else None
    report["bot_log_lines_in_window"] = len(lines)

    # Filters
    filter_counter: Counter[str] = Counter()
    setup_filter_map: dict[str, Counter[str]] = defaultdict(Counter)
    for line in lines:
        m = RE_FILTERED.search(line)
        if m:
            setup, direction, score, reason = m.group(1), m.group(2), m.group(3), m.group(4)
            filter_counter[reason] += 1
            setup_filter_map[setup][reason] += 1

    # Signal diagnostics summaries
    diag_entries = []
    for line in lines:
        m = RE_SIGNAL_DIAG.search(line)
        if m:
            try:
                rejects = _parse_filter_rejects_json(m.group(5))
            except Exception:
                rejects = []
            diag_entries.append(
                {
                    "detector_runs": int(m.group(1)),
                    "hits": int(m.group(2)),
                    "candidates": int(m.group(3)),
                    "hit_rate": float(m.group(4)),
                    "top_rejects": rejects,
                }
            )

    # Delivered signals
    delivered = []
    for line in lines:
        m = RE_DELIVERED.search(line)
        if m:
            delivered.append({"signal_id": m.group(1), "setup": m.group(2)})

    # Cycles
    cycle_errors = 0
    cycle_ok = 0
    total_candidates = 0
    total_delivered_cycles = 0
    for line in lines:
        m = RE_CYCLE.search(line)
        if m:
            candidates, delivered_c, rejected, status = (
                int(m.group(2)),
                int(m.group(3)),
                int(m.group(4)),
                m.group(5),
            )
            total_candidates += candidates
            total_delivered_cycles += delivered_c
            if status != "ok":
                cycle_errors += 1
            else:
                cycle_ok += 1

    # SL-rate penalties applied
    sl_penalties: list[dict[str, Any]] = []
    for line in lines:
        m = RE_SL_PENALTY.search(line)
        if m:
            sl_penalties.append(
                {
                    "setup": m.group(1),
                    "sl_rate": float(m.group(2)),
                    "score_change": m.group(3),
                }
            )

    # ── API / WS / Data quality ─────────────────────────────────────────────

    degraded_symbols: Counter[str] = Counter()
    degraded_reasons: Counter[str] = Counter()
    for line in lines:
        m = RE_DEGRADED.search(line)
        if m:
            degraded_symbols[m.group(1)] += 1
            reason_text = m.group(2).strip()
            # shorten reason
            if "rate_limit_paused" in reason_text:
                degraded_reasons["rate_limit_paused"] += 1
            elif "unavailable" in reason_text:
                degraded_reasons["data_unavailable"] += 1
            else:
                degraded_reasons[reason_text[:60]] += 1

    rate_limit_pauses = []
    for line in lines:
        m = RE_RATE_LIMIT.search(line)
        if m:
            rate_limit_pauses.append(int(m.group(1)))

    ws_events = []
    for line in lines:
        m = RE_WS_EVENT.search(line)
        if m:
            ws_events.append(line.strip()[:120])

    benchmark_stale: Counter[str] = Counter()
    for line in lines:
        m = RE_BENCHMARK_STALE.search(line)
        if m:
            benchmark_stale[m.group(1)] += 1

    # Errors & warnings
    errors: list[str] = []
    warnings: list[str] = []
    exception_blocks: list[str] = []
    for i, line in enumerate(lines):
        if RE_ERROR.search(line):
            errors.append(line.strip()[:200])
        if RE_WARNING.search(line):
            warnings.append(line.strip()[:200])
        if RE_EXCEPTION.search(line):
            exception_blocks.append(line.strip()[:200])

    # ── Snapshot stats ──────────────────────────────────────────────────────
    watch_dir = _latest_live_watch()
    snapshots = _latest_snapshots(watch_dir) if watch_dir else []
    # Last snapshot
    last_snap = snapshots[-1] if snapshots else {}
    snap_runtime = last_snap.get("runtime", {})

    # ── Assemble report ─────────────────────────────────────────────────────

    report["signals"] = {
        "delivered_this_window": len(delivered),
        "delivered_details": delivered[:10],
        "total_delivered_session": snap_runtime.get("delivered_total", "?"),
        "total_cycles_session": snap_runtime.get("cycles_total", "?"),
        "candidates_session": snap_runtime.get("candidates_total", "?"),
        "rejected_session": snap_runtime.get("rejected_total", "?"),
        "candidates_window": total_candidates,
        "cycle_errors_window": cycle_errors,
        "cycles_ok_window": cycle_ok,
    }

    report["filters"] = {
        "reject_reasons": dict(filter_counter.most_common(10)),
        "by_setup": {
            k: dict(v.most_common(5))
            for k, v in sorted(
                setup_filter_map.items(), key=lambda x: sum(x[1].values()), reverse=True
            )[:8]
        },
        "sl_penalties_applied": len(sl_penalties),
        "sl_penalties_details": sl_penalties[:5],
        "signal_diagnostics": diag_entries[-1] if diag_entries else None,
    }

    report["api_ws"] = {
        "enrichment_degraded_symbols": dict(degraded_symbols.most_common(10)),
        "degradation_reasons": dict(degraded_reasons),
        "rate_limit_pauses": len(rate_limit_pauses),
        "max_rate_limit_pause_s": max(rate_limit_pauses) if rate_limit_pauses else 0,
        "ws_events": ws_events[:5],
        "benchmark_stale_symbols": dict(benchmark_stale.most_common(8)),
    }

    report["health"] = {
        "errors_count": len(errors),
        "warnings_count": len(warnings),
        "exception_blocks": len(exception_blocks),
        "errors_sample": errors[:5],
        "warnings_sample": warnings[:3],
        "exception_sample": exception_blocks[:3],
    }

    # ── Anomaly detection ───────────────────────────────────────────────────
    anomalies: list[str] = []

    if snap_runtime.get("delivered_total", 0) == 0 and snap_runtime.get("cycles_total", 0) > 200:
        anomalies.append(
            "ZERO_DELIVERIES: Bot ran >200 cycles but delivered 0 signals this session"
        )

    if len(rate_limit_pauses) > 0:
        anomalies.append(
            f"RATE_LIMIT: {len(rate_limit_pauses)} rate-limit hits in window, max pause {max(rate_limit_pauses)}s"
        )

    if len(errors) > 5:
        anomalies.append(
            f"HIGH_ERROR_RATE: {len(errors)} ERROR lines in {lookback_seconds}s window"
        )

    if len(exception_blocks) > 0:
        anomalies.append(f"EXCEPTIONS: {len(exception_blocks)} exception/traceback lines detected")

    if len(ws_events) > 3:
        anomalies.append(f"WS_INSTABILITY: {len(ws_events)} WebSocket events in window")

    if len(degraded_symbols) > 5:
        anomalies.append(
            f"DATA_DEGRADATION: {len(degraded_symbols)} symbols with enrichment degradation"
        )

    # Check if hit_rate suspiciously low
    if diag_entries:
        last_diag = diag_entries[-1]
        if last_diag["hit_rate"] < 0.005 and last_diag["detector_runs"] > 1000:
            anomalies.append(
                f"LOW_HIT_RATE: hit_rate={last_diag['hit_rate']:.4f} — strategies firing but almost nothing passing"
            )

    # SL penalty flood
    sl_setups_high = {
        s: c for s, c in [(p["setup"], p["sl_rate"]) for p in sl_penalties] if c >= 1.0
    }
    if sl_setups_high:
        anomalies.append(
            f"SL_PENALTY_100PCT: setups with 100% SL rate: {list(sl_setups_high.keys())[:5]}"
        )

    # Filter domination
    if filter_counter:
        top_reason, top_count = filter_counter.most_common(1)[0]
        total_filtered = sum(filter_counter.values())
        if total_filtered > 0 and top_count / total_filtered > 0.7:
            anomalies.append(
                f"FILTER_DOMINATED: '{top_reason}' accounts for {top_count}/{total_filtered} ({100 * top_count // total_filtered}%) of all rejects"
            )

    report["anomalies"] = anomalies

    return report


# ── Formatting ───────────────────────────────────────────────────────────────


def _fmt_report(r: dict[str, Any]) -> str:
    lines = []
    ts = r["generated_at"][:19].replace("T", " ")
    window_min = r["lookback_seconds"] // 60
    lines.append(f"\n{'=' * 70}")
    lines.append(f"  SIGNAL BOT MONITOR — {ts} UTC  (last {window_min} min)")
    lines.append(f"{'=' * 70}")

    # Signals
    sig = r["signals"]
    lines.append("\n📊 SIGNALS")
    lines.append(f"  Delivered (window):  {sig['delivered_this_window']}")
    lines.append(f"  Delivered (session): {sig['total_delivered_session']}")
    lines.append(f"  Candidates (window): {sig['candidates_window']}")
    lines.append(f"  Cycles OK/Error:     {sig['cycles_ok_window']}/{sig['cycle_errors_window']}")
    lines.append(
        f"  Session cycles:      {sig['total_cycles_session']}  rejected={sig['rejected_session']}"
    )
    if sig["delivered_details"]:
        lines.append("  Last delivered signals:")
        for d in sig["delivered_details"][:5]:
            lines.append(f"    → {d['setup']} [{d['signal_id']}]")

    # Filters
    flt = r["filters"]
    lines.append(f"\n🔽 FILTERS  (sl_penalties={flt['sl_penalties_applied']})")
    if flt["reject_reasons"]:
        lines.append("  Top rejection reasons:")
        for reason, cnt in list(flt["reject_reasons"].items())[:7]:
            lines.append(f"    {reason:<35} {cnt}")
    if flt["signal_diagnostics"]:
        d = flt["signal_diagnostics"]
        lines.append(
            f"  Last diag: runs={d['detector_runs']} hits={d['hits']} "
            f"candidates={d['candidates']} hit_rate={d['hit_rate']:.4f}"
        )
    if flt["sl_penalties_details"]:
        lines.append("  SL-rate penalties (sample):")
        for p in flt["sl_penalties_details"][:3]:
            lines.append(
                f"    {p['setup']:<35} sl_rate={p['sl_rate']:.2f}  score {p['score_change']}"
            )

    # API / WS
    api = r["api_ws"]
    lines.append("\n🌐 API / WS")
    lines.append(
        f"  Rate-limit pauses:   {api['rate_limit_pauses']}  (max {api['max_rate_limit_pause_s']}s)"
    )
    lines.append(f"  Degraded symbols:    {len(api['enrichment_degraded_symbols'])}")
    if api["degradation_reasons"]:
        for reason, cnt in api["degradation_reasons"].items():
            lines.append(f"    {reason:<40} {cnt}")
    if api["benchmark_stale_symbols"]:
        lines.append(f"  Benchmark-stale:     {len(api['benchmark_stale_symbols'])} symbols")
    if api["ws_events"]:
        lines.append(f"  WS events ({len(api['ws_events'])}):")
        for ev in api["ws_events"][:3]:
            lines.append(f"    {ev[:100]}")

    # Health
    health = r["health"]
    lines.append("\n🩺 HEALTH")
    lines.append(
        f"  Errors:    {health['errors_count']}  |  Warnings: {health['warnings_count']}  |  Exceptions: {health['exception_blocks']}"
    )
    if health["errors_sample"]:
        lines.append("  Error sample:")
        for e in health["errors_sample"][:3]:
            lines.append(f"    ⚠ {e[:120]}")

    # Anomalies
    anomalies = r["anomalies"]
    if anomalies:
        lines.append(f"\n🚨 ANOMALIES ({len(anomalies)})")
        for a in anomalies:
            lines.append(f"  ❗ {a}")
    else:
        lines.append("\n✅ No anomalies detected")

    lines.append(f"\n{'=' * 70}\n")
    return "\n".join(lines)


# ── Main ─────────────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--lookback", type=int, default=300, help="Seconds to look back (default 300 = 5min)"
    )
    parser.add_argument("--config", default="config.toml")
    parser.add_argument("--json", action="store_true", help="Output raw JSON")
    args = parser.parse_args()

    report = analyze(lookback_seconds=args.lookback)

    # Always print human report
    print(_fmt_report(report))

    # Append JSON snapshot
    MONITOR_OUT.parent.mkdir(parents=True, exist_ok=True)
    with MONITOR_OUT.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(report) + "\n")

    # If --json also dump to stdout after report
    if args.json:
        print(json.dumps(report, indent=2))

    # Exit non-zero if anomalies found (useful for CI / alerting)
    sys.exit(1 if report["anomalies"] else 0)


if __name__ == "__main__":
    main()
