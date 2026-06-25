"""Session telemetry, live_watch bridge, runtime log analysis."""

from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from engine.domain.config import _ALL_SETUP_IDS


# --- from telemetry_strategy_analysis.py ---
def read_analysis_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def analyze_decision_rows(
    decisions: list[dict[str, Any]],
    *,
    shortlist_rows: list[dict[str, Any]] | None = None,
    build_rows: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Aggregate strategy decision dicts (JSONL or stdout-recovered)."""
    shortlist_rows = shortlist_rows or []
    build_rows = build_rows or []
    latest_shortlist = shortlist_rows[-1] if shortlist_rows else {}
    fit_counts = latest_shortlist.get("strategy_fit_counts") or {}

    by_strategy: dict[str, Counter[str]] = defaultdict(Counter)
    symbols_by_strategy: dict[str, set[str]] = defaultdict(set)
    for row in decisions:
        sid = str(row.get("setup_id") or row.get("strategy") or "unknown")
        decision = str(row.get("status") or row.get("decision") or "unknown").lower()
        symbol = str(row.get("symbol") or "").upper()
        by_strategy[sid][decision] += 1
        if symbol:
            symbols_by_strategy[sid].add(symbol)
        reason = str(row.get("reason_code") or "")
        if reason:
            by_strategy[sid][f"reason:{reason}"] += 1

    skip_not_routed = sum(
        1
        for row in decisions
        if str(row.get("reason_code") or "") == "asset_fit.shortlist_not_routed"
    )
    strategy_rows: list[dict[str, Any]] = []
    for setup_id in _ALL_SETUP_IDS:
        counts = by_strategy.get(setup_id, Counter())
        strategy_rows.append(
            {
                "setup_id": setup_id,
                "symbols_touched": len(symbols_by_strategy.get(setup_id, set())),
                "signal": counts.get("signal", 0),
                "reject": counts.get("reject", 0),
                "skip": counts.get("skip", 0),
                "not_routed": counts.get("reason:asset_fit.shortlist_not_routed", 0),
                "shortlist_fit_symbols": int(fit_counts.get(setup_id, 0) or 0),
                "ran": counts.get("signal", 0) + counts.get("reject", 0) + counts.get("skip", 0)
                > 0,
            }
        )

    return {
        "decision_rows": len(decisions),
        "skip_not_routed_total": skip_not_routed,
        "latest_shortlist": latest_shortlist,
        "latest_build": build_rows[-1] if build_rows else {},
        "strategies": strategy_rows,
        "strategies_ran": sum(1 for row in strategy_rows if row["ran"]),
        "strategies_zero_runs": [row["setup_id"] for row in strategy_rows if not row["ran"]],
    }


def analyze_telemetry(analysis_dir: Path) -> dict[str, Any]:
    """Aggregate strategy_decisions + shortlist funnel for all 38 setups."""
    decisions = read_analysis_jsonl(analysis_dir / "strategy_decisions.jsonl")
    shortlist_rows = read_analysis_jsonl(analysis_dir / "shortlist.jsonl")
    build_rows = read_analysis_jsonl(analysis_dir / "shortlist_build.jsonl")
    return analyze_decision_rows(
        decisions,
        shortlist_rows=shortlist_rows,
        build_rows=build_rows,
    )


def build_zero_hit_triage(live_telemetry: dict[str, Any] | None) -> dict[str, Any]:
    """Classify zero-run setups for calibration (no threshold changes)."""
    if not live_telemetry:
        return {"zero_runs": [], "not_routed_heavy": [], "fit_but_no_runs": []}
    strategies = list(live_telemetry.get("strategies") or [])
    zero_runs = list(live_telemetry.get("strategies_zero_runs") or [])
    not_routed_heavy = [
        row["setup_id"]
        for row in strategies
        if int(row.get("not_routed", 0) or 0) >= 5 and not row.get("ran")
    ]
    fit_but_no_runs = [
        row["setup_id"]
        for row in strategies
        if int(row.get("shortlist_fit_symbols", 0) or 0) > 0 and not row.get("ran")
    ]
    return {
        "zero_runs": zero_runs,
        "not_routed_heavy": not_routed_heavy,
        "fit_but_no_runs": fit_but_no_runs,
        "decision_rows": int(live_telemetry.get("decision_rows") or 0),
        "strategies_ran": int(live_telemetry.get("strategies_ran") or 0),
    }


# --- from runtime_analysis.py ---
if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path


def file_has_rows(path: Path) -> bool:
    if not path.exists():
        return False
    try:
        with path.open("r", encoding="utf-8") as handle:
            return any(line.strip() for line in handle)
    except OSError:
        return False


def find_latest_run_dir(
    telemetry_dir: Path,
    run_id: str | None = None,
    interesting_files: tuple[str, ...] = (
        "strategy_decisions.jsonl",
        "symbol_analysis.jsonl",
        "rejected.jsonl",
        "cycles.jsonl",
    ),
) -> Path | None:
    """Return the latest run directory that has non-empty analysis artifacts."""
    runs_dir = telemetry_dir / "runs"
    if not runs_dir.exists():
        return None

    if run_id:
        explicit = runs_dir / run_id
        return explicit if explicit.exists() else None

    run_dirs = sorted(
        (path for path in runs_dir.iterdir() if path.is_dir()),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )

    fallback: Path | None = None
    for run_dir in run_dirs:
        analysis_dir = run_dir / "analysis"
        if not analysis_dir.exists():
            continue
        fallback = fallback or run_dir
        if any(file_has_rows(analysis_dir / filename) for filename in interesting_files):
            return run_dir
    return fallback


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    """Read JSONL rows, skipping empty lines and malformed JSON."""
    if not path.exists():
        return []

    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            raw = line.strip()
            if not raw:
                continue
            try:
                row = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                rows.append(row)
    return rows


def aggregate_rejection_funnel(rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    stats: dict[str, Any] = {
        "by_stage": Counter(),
        "by_setup": Counter(),
        "by_reason": Counter(),
        "by_symbol_setup": Counter(),
        "detailed": defaultdict(list),
    }

    for row in rows:
        stage = str(row.get("stage") or "unknown")
        setup = str(row.get("setup_id") or "unknown")
        reason = str(row.get("reason") or "unknown")
        symbol = str(row.get("symbol") or "unknown")

        stats["by_stage"][stage] += 1
        stats["by_setup"][setup] += 1
        stats["by_reason"][reason] += 1
        stats["by_symbol_setup"][f"{symbol}:{setup}"] += 1

        if len(stats["detailed"][reason]) < 3:
            sample: dict[str, Any] = {
                "symbol": symbol,
                "setup": setup,
                "stage": stage,
            }
            if "adx_1h" in row:
                sample["adx_1h"] = row["adx_1h"]
            if "risk_reward" in row:
                sample["rr"] = row["risk_reward"]
            if "trend_direction" in row:
                sample["trend"] = row["trend_direction"]
            if "details" in row:
                sample["details"] = row["details"]
            stats["detailed"][reason].append(sample)

    return stats


def aggregate_symbol_funnel(rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    stats: dict[str, Any] = {
        "symbols_processed": 0,
        "symbols_with_raw_hits": 0,
        "total_raw_hits": 0,
        "total_candidates": 0,
        "total_delivered": 0,
        "rejection_reasons": Counter(),
    }

    for data in rows:
        funnel = data.get("funnel", {}) if isinstance(data.get("funnel"), dict) else {}

        stats["symbols_processed"] += 1
        raw_hits = int(funnel.get("raw_hits", 0) or 0)
        candidates = int(data.get("candidates", 0) or 0)

        if raw_hits > 0:
            stats["symbols_with_raw_hits"] += 1
            stats["total_raw_hits"] += raw_hits

        stats["total_candidates"] += candidates
        stats["total_delivered"] += int(data.get("delivered", 0) or 0)

        if raw_hits > 0 and candidates == 0:
            for key, value in funnel.items():
                if key == "alignment_penalties" and isinstance(value, int) and value > 0:
                    stats["rejection_reasons"]["alignment_penalties"] += value
                elif "rejects" in str(key) and isinstance(value, int) and value > 0:
                    stats["rejection_reasons"][str(key)] += value

    return stats


def aggregate_cycle_stats(rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    stats: dict[str, Any] = {
        "total_cycles": 0,
        "symbols_analyzed": set(),
        "total_detector_runs": 0,
        "total_candidates": 0,
        "total_delivered": 0,
        "health_checks": 0,
        "by_symbol": defaultdict(
            lambda: {"cycles": 0, "detectors": 0, "candidates": 0, "delivered": 0}
        ),
    }

    for row in rows:
        symbol = row.get("symbol")
        if symbol:
            symbol_str = str(symbol)
            detector_runs = int(row.get("detector_runs", 0) or 0)
            candidates = int(row.get("candidates", 0) or 0)
            delivered = int(row.get("delivered", 0) or 0)

            stats["total_cycles"] += 1
            stats["symbols_analyzed"].add(symbol_str)
            stats["total_detector_runs"] += detector_runs
            stats["total_candidates"] += candidates
            stats["total_delivered"] += delivered

            symbol_stats = stats["by_symbol"][symbol_str]
            symbol_stats["cycles"] += 1
            symbol_stats["detectors"] += detector_runs
            symbol_stats["candidates"] += candidates
            symbol_stats["delivered"] += delivered
            continue

        stats["health_checks"] += 1
        funnel = row.get("funnel") if isinstance(row.get("funnel"), dict) else {}
        if funnel:
            stats["total_detector_runs"] += int(funnel.get("detector_runs", 0) or 0)
            stats["total_candidates"] += int(funnel.get("post_filter_candidates", 0) or 0)
            stats["total_delivered"] += int(funnel.get("delivered", 0) or 0)

    return stats


def parse_cycle_log_lines(lines: Iterable[str]) -> dict[str, Any]:
    """Parse cycle lines from text logs into deltas for monitor stats."""
    parsed: dict[str, Any] = {
        "cycles": 0,
        "symbols_processed": set(),
        "detector_runs_total": 0,
        "candidates_total": 0,
        "delivered_total": 0,
        "rejected_total": 0,
        "symbols_with_candidates": [],
        "last_signals": [],
        "errors": [],
    }

    for line in lines:
        symbol: str | None = None
        if "cycle | symbol=" in line:
            try:
                parts = line.split("|")
                for part in parts:
                    if "symbol=" in part:
                        symbol = part.split("symbol=")[1].split()[0]
                        parsed["symbols_processed"].add(symbol)
                    if "detector_runs=" in part:
                        parsed["detector_runs_total"] += int(
                            part.split("detector_runs=")[1].split()[0]
                        )
                    if "candidates=" in part:
                        candidates = int(part.split("candidates=")[1].split()[0])
                        parsed["candidates_total"] += candidates
                        if candidates > 0:
                            parsed["symbols_with_candidates"].append(
                                {"symbol": symbol, "candidates": candidates}
                            )
                    if "delivered=" in part:
                        delivered = int(part.split("delivered=")[1].split()[0])
                        parsed["delivered_total"] += delivered
                        if delivered > 0:
                            parsed["last_signals"].append(
                                {"symbol": symbol, "delivered": delivered}
                            )
                    if "rejected=" in part:
                        parsed["rejected_total"] += int(part.split("rejected=")[1].split()[0])
                parsed["cycles"] += 1
            except (IndexError, ValueError):
                continue

        if "ERROR" in line or "error" in line.lower():
            parsed["errors"].append(line[:200])

    return parsed


_STRATEGY_DECISION_PIPE = re.compile(
    r"strategy_decision\s*\|\s*symbol=(?P<symbol>\S+)\s+setup_id=(?P<setup_id>\S+)"
    r"\s+status=(?P<status>\S+)\s+reason_code=(?P<reason_code>\S+)"
    r"(?:\s+trigger=(?P<trigger>\S+))?",
)
_STRATEGY_NO_SIGNAL = re.compile(
    r"(?P<symbol>\S+):\s+strategy produced no signal\s*\|\s+setup=(?P<setup_id>\S+)"
    r"\s+status=(?P<status>\S+)\s+reason=(?P<reason_code>.+)$",
)


def parse_strategy_decision_log_lines(lines: Iterable[str]) -> list[dict[str, Any]]:
    """Recover strategy_decision rows from bot stdout when telemetry JSONL is missing."""
    rows: list[dict[str, Any]] = []
    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        if line.startswith("{") and '"setup_id"' in line:
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict) and payload.get("setup_id"):
                rows.append(payload)
            continue
        match = _STRATEGY_DECISION_PIPE.search(line)
        if match is None:
            match = _STRATEGY_NO_SIGNAL.search(line)
        if match is None:
            continue
        groups = match.groupdict()
        rows.append(
            {
                "symbol": groups.get("symbol"),
                "setup_id": groups.get("setup_id"),
                "status": groups.get("status"),
                "reason_code": (groups.get("reason_code") or "").strip(),
                "trigger": groups.get("trigger"),
            }
        )
    return rows


# --- from live_watch.py ---


def find_live_watch_session(
    live_watch_root: Path,
    run_id: str,
) -> Path | None:
    """Return session directory ``live_watch/{run_id}`` when present."""
    if not run_id:
        return None
    direct = live_watch_root / run_id
    if direct.is_dir():
        return direct
    for path in sorted(live_watch_root.glob(f"{run_id}*"), reverse=True):
        if path.is_dir():
            return path
    return None


def find_latest_rollup(live_watch_root: Path) -> Path | None:
    rollups = sorted(live_watch_root.glob("rollup_*.json"), reverse=True)
    return rollups[0] if rollups else None


def resolve_telemetry_analysis_dir(
    *,
    run_id: str,
    telemetry_dir: Path,
    live_watch_dir: Path | None = None,
) -> tuple[Path | None, str]:
    """Prefer ``telemetry/runs/{id}/analysis``; else None (use live_watch summary)."""
    runs_dir = telemetry_dir / "runs"
    if run_id:
        explicit = runs_dir / run_id / "analysis"
        if explicit.is_dir() and any(
            file_has_rows(explicit / name)
            for name in (
                "strategy_decisions.jsonl",
                "symbol_analysis.jsonl",
                "rejected.jsonl",
                "cycles.jsonl",
            )
        ):
            return explicit, "telemetry"
    latest = find_latest_run_dir(telemetry_dir, run_id=run_id or None)
    if latest is not None:
        analysis = latest / "analysis"
        if analysis.is_dir():
            return analysis, "telemetry"
    if live_watch_dir is not None and run_id:
        session = find_live_watch_session(live_watch_dir, run_id)
        if session is not None:
            return None, "live_watch"
    return None, "none"


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return read_jsonl(path)


def _parse_session_started_epoch(session_meta: dict[str, Any], session_dir: Path) -> float | None:
    started = session_meta.get("started_at")
    if isinstance(started, str) and started:
        try:
            normalized = started.replace("Z", "+00:00")
            return datetime.fromisoformat(normalized).timestamp()
        except ValueError:
            pass
    try:
        return session_dir.stat().st_mtime
    except OSError:
        return None


def find_telemetry_run_for_session(
    telemetry_dir: Path,
    *,
    session_started_epoch: float | None,
    max_skew_seconds: float = 900.0,
) -> Path | None:
    """Pick the telemetry run whose directory mtime best matches a supervised session."""
    runs_dir = telemetry_dir / "runs"
    if not runs_dir.is_dir():
        return None
    candidates: list[tuple[float, Path]] = []
    for run_dir in runs_dir.iterdir():
        if not run_dir.is_dir():
            continue
        analysis = run_dir / "analysis"
        if not analysis.is_dir():
            continue
        try:
            mtime = run_dir.stat().st_mtime
        except OSError:
            continue
        candidates.append((mtime, run_dir))
    if not candidates:
        return None
    if session_started_epoch is None:
        return max(candidates, key=lambda item: item[0])[1]
    best: tuple[float, Path] | None = None
    for mtime, run_dir in candidates:
        skew = abs(mtime - session_started_epoch)
        if skew > max_skew_seconds:
            continue
        if best is None or skew < best[0]:
            best = (skew, run_dir)
    if best is not None:
        return best[1]
    return max(candidates, key=lambda item: item[0])[1]


def summarize_live_watch_session(
    session_dir: Path,
    *,
    telemetry_dir: Path | None = None,
) -> dict[str, Any]:
    """Aggregate totals from ``session_summary.json`` and last snapshot."""
    summary_path = session_dir / "session_summary.json"
    session_meta: dict[str, Any] = {}
    if summary_path.is_file():
        try:
            session_meta = json.loads(summary_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            session_meta = {}

    snapshots = _read_jsonl(session_dir / "snapshots.jsonl")
    last_snap = snapshots[-1] if snapshots else {}
    runtime = dict(last_snap.get("runtime") or {})
    tracking = dict(last_snap.get("tracking") or {})

    telemetry_run: str | None = None
    telemetry_source = "none"
    note = "Per-strategy JSONL not stored under live_watch; use telemetry/runs when available."
    strategy_detail: dict[str, Any] | None = None

    if telemetry_dir is not None:
        started_epoch = _parse_session_started_epoch(session_meta, session_dir)
        run_dir = find_telemetry_run_for_session(
            telemetry_dir,
            session_started_epoch=started_epoch,
        )
        if run_dir is not None:
            analysis = run_dir / "analysis"
            if analysis.is_dir():
                strategy_detail = analyze_telemetry(analysis)
                telemetry_run = run_dir.name
                telemetry_source = "telemetry"
                note = "Linked telemetry run with full per-setup strategy matrix."

    if strategy_detail is None:
        stdout_path = session_dir / "bot_stdout.log"
        if stdout_path.is_file():
            try:
                stdout_text = stdout_path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                stdout_text = ""
            recovered = parse_strategy_decision_log_lines(stdout_text.splitlines())
            if recovered:
                strategy_detail = analyze_decision_rows(recovered)
                telemetry_source = "bot_stdout"
                note = (
                    "Recovered strategy_decisions from bot_stdout.log "
                    "(telemetry JSONL unavailable for this session)."
                )

    decision_rows = int((strategy_detail or {}).get("decision_rows") or 0)
    strategies_ran = int((strategy_detail or {}).get("strategies_ran") or 0)

    payload: dict[str, Any] = {
        "source": "live_watch",
        "session_dir": str(session_dir),
        "run_id": session_meta.get("run_id") or session_dir.name,
        "started_at": session_meta.get("started_at"),
        "ended_at": session_meta.get("ended_at"),
        "minutes": session_meta.get("minutes"),
        "snapshots": session_meta.get("snapshots") or len(snapshots),
        "bot_exit_code": session_meta.get("bot_exit_code"),
        "total_log_errors_seen": session_meta.get("total_log_errors_seen"),
        "total_strategy_error_lines": session_meta.get("total_strategy_error_lines"),
        "cycles_total": runtime.get("cycles_total"),
        "candidates_total": runtime.get("candidates_total"),
        "delivered_total": runtime.get("delivered_total"),
        "rejected_total": runtime.get("rejected_total"),
        "detector_runs_total": runtime.get("detector_runs_total"),
        "symbols_processed_count": len(runtime.get("symbols") or []),
        "tracking_db": tracking.get("db"),
        "telemetry_run": telemetry_run,
        "telemetry_source": telemetry_source,
        "decision_rows": decision_rows,
        "strategies_ran": strategies_ran,
        "note": note,
    }
    if strategy_detail is not None:
        payload["strategies"] = strategy_detail.get("strategies")
        payload["strategies_zero_runs"] = strategy_detail.get("strategies_zero_runs")
        payload["latest_build"] = strategy_detail.get("latest_build")
        payload["latest_shortlist"] = strategy_detail.get("latest_shortlist")
        payload["skip_not_routed_total"] = strategy_detail.get("skip_not_routed_total")
    return payload


def summarize_rollup(rollup_path: Path) -> dict[str, Any]:
    payload = json.loads(rollup_path.read_text(encoding="utf-8"))
    sessions = list(payload.get("sessions") or [])
    totals = {
        "session_count": len(sessions),
        "delivered_total": 0,
        "cycles_total": 0,
        "snapshots": 0,
        "strategy_error_lines": 0,
    }
    session_rows: list[dict[str, Any]] = []
    for session in sessions:
        last = session.get("last_snapshot") or {}
        runtime = last.get("runtime") or {}
        delivered = int(runtime.get("delivered_total") or 0)
        cycles = int(runtime.get("cycles_total") or 0)
        totals["delivered_total"] += delivered
        totals["cycles_total"] = max(totals["cycles_total"], cycles)
        totals["snapshots"] += int(session.get("snapshots") or 0)
        totals["strategy_error_lines"] += int(session.get("total_strategy_error_lines") or 0)
        session_rows.append(
            {
                "run_id": session.get("run_id"),
                "minutes": session.get("minutes"),
                "bot_exit_code": session.get("bot_exit_code"),
                "delivered_total": delivered,
                "cycles_total": cycles,
                "snapshots": session.get("snapshots"),
            }
        )
    return {
        "rollup_path": str(rollup_path),
        "generated_at": payload.get("generated_at"),
        "totals": totals,
        "sessions": session_rows,
    }
