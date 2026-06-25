"""Outcome validator — ground-truth fate of deep signals vs the model.

Reads ``data/evidence_trace.jsonl`` (directional signals only), resolves each
against real Binance klines: which of TP1 / SL was touched first (intrabar
hi/lo), evaluated at 6h / 12h / 24h / 36h horizons. Aggregates a win-rate table
per playbook (path type) and direction.

This is the ground-truth contour the model self-scores cannot provide. Run:

    .venv/bin/python -m hunt_core._dev.validate_outcomes
    .venv/bin/python -m hunt_core._dev.validate_outcomes --horizon 24h --min-age-h 24

100% CCXT market plane (``fetch_klines_sync``). Public data only.
"""
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from datetime import UTC, datetime
from typing import Any

from hunt_core.market.factory import fetch_klines_sync
from hunt_core.paths import EVIDENCE_TRACE_JSONL

_HORIZONS_H = {"6h": 6, "12h": 12, "24h": 24, "36h": 36}
_RESOLVE_INTERVAL = "5m"  # fine-grained so intrabar first-touch is accurate


def _parse_ts(ts: str) -> datetime | None:
    try:
        return datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return None


def _load_signals(min_age_h: float) -> list[dict[str, Any]]:
    if not EVIDENCE_TRACE_JSONL.exists():
        return []
    now = datetime.now(UTC)
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for line in EVIDENCE_TRACE_JSONL.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if rec.get("action") not in {"long", "short"}:
            continue
        sid = str(rec.get("signal_id") or "")
        if sid and sid in seen:
            continue
        ts = _parse_ts(str(rec.get("ts") or ""))
        if ts is None:
            continue
        age_h = (now - ts).total_seconds() / 3600.0
        if age_h < min_age_h:
            continue
        zo = rec.get("zone_origin") or {}
        if not zo.get("tp1") or not zo.get("stop_loss"):
            continue
        rec["_ts"] = ts
        out.append(rec)
        if sid:
            seen.add(sid)
    return out


def _first_touch(
    klines: list[list[Any]],
    *,
    direction: str,
    tp1: float,
    sl: float,
    until_ms: int,
) -> str:
    """'tp' / 'sl' / 'none' — whichever level the wick touches first."""
    for k in klines:
        ts_ms, _o, hi, lo, _c = int(k[0]), k[1], float(k[2]), float(k[3]), k[4]
        if ts_ms > until_ms:
            break
        if direction == "long":
            sl_hit = lo <= sl
            tp_hit = hi >= tp1
        else:
            sl_hit = hi >= sl
            tp_hit = lo <= tp1
        # Conservative: if a single bar straddles both, count SL first.
        if sl_hit:
            return "sl"
        if tp_hit:
            return "tp"
    return "none"


def resolve(min_age_h: float, horizon: str, proxy_url: str | None, trust_env: bool) -> None:
    hz_h = _HORIZONS_H[horizon]
    signals = _load_signals(min_age_h)
    if not signals:
        print(f"No directional signals with age >= {min_age_h}h in {EVIDENCE_TRACE_JSONL.name}.")
        return

    # tally[playbook][direction] = {tp, sl, none}
    tally: dict[str, dict[str, dict[str, int]]] = defaultdict(
        lambda: defaultdict(lambda: {"tp": 0, "sl": 0, "none": 0})
    )
    resolved = 0
    for rec in signals:
        sym = str(rec.get("symbol") or "")
        direction = str(rec.get("action") or "")
        playbook = str(rec.get("playbook") or "?")
        zo = rec.get("zone_origin") or {}
        tp1 = float(zo.get("tp1") or 0)
        sl = float(zo.get("stop_loss") or 0)
        ts: datetime = rec["_ts"]
        since_ms = int(ts.timestamp() * 1000)
        until_ms = since_ms + hz_h * 3600 * 1000
        try:
            klines = fetch_klines_sync(
                sym, _RESOLVE_INTERVAL,
                since_ms=since_ms, until_ms=until_ms,
                limit=1500, proxy_url=proxy_url, trust_env=trust_env,
            )
        except Exception as exc:  # noqa: BLE001 — offline tool, report and skip
            print(f"  ! {sym} fetch failed: {exc}")
            continue
        if not klines:
            continue
        outcome = _first_touch(klines, direction=direction, tp1=tp1, sl=sl, until_ms=until_ms)
        tally[playbook][direction][outcome] += 1
        resolved += 1

    print(f"\n=== Outcome validation @ {horizon} (resolved {resolved}/{len(signals)}) ===")
    print(f"{'playbook':28} {'dir':5} {'n':>4} {'TP':>4} {'SL':>4} {'none':>5} {'win%':>6}")
    print("-" * 64)
    g_tp = g_sl = g_none = 0
    for playbook in sorted(tally):
        for direction in sorted(tally[playbook]):
            c = tally[playbook][direction]
            n = c["tp"] + c["sl"] + c["none"]
            decided = c["tp"] + c["sl"]
            win = (c["tp"] / decided * 100.0) if decided else 0.0
            g_tp += c["tp"]; g_sl += c["sl"]; g_none += c["none"]
            print(f"{playbook:28.28} {direction:5} {n:>4} {c['tp']:>4} {c['sl']:>4} {c['none']:>5} {win:>5.0f}%")
    print("-" * 64)
    g_decided = g_tp + g_sl
    g_win = (g_tp / g_decided * 100.0) if g_decided else 0.0
    print(f"{'TOTAL':28} {'':5} {g_tp + g_sl + g_none:>4} {g_tp:>4} {g_sl:>4} {g_none:>5} {g_win:>5.0f}%")
    if g_none:
        print(f"\nNote: {g_none} signals reached neither TP1 nor SL within {horizon} (unresolved).")


def main() -> None:
    ap = argparse.ArgumentParser(description="Validate deep-signal outcomes vs real klines.")
    ap.add_argument("--horizon", choices=sorted(_HORIZONS_H), default="24h")
    ap.add_argument("--min-age-h", type=float, default=None,
                    help="Only resolve signals older than this (default = horizon hours).")
    ap.add_argument("--proxy-url", default=None)
    ap.add_argument("--no-trust-env", action="store_true")
    args = ap.parse_args()
    min_age = args.min_age_h if args.min_age_h is not None else float(_HORIZONS_H[args.horizon])
    resolve(min_age, args.horizon, args.proxy_url, not args.no_trust_env)


if __name__ == "__main__":
    main()
