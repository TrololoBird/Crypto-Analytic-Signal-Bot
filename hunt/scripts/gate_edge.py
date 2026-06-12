#!/usr/bin/env python3
"""Measure the confirmation gate's edge: do CONFIRMED setups beat the raw 52% SL?

The tick JSONL (dump_minute_watch*.jsonl) stores, per tick, whether the live
confirm gate passed (`dump.confirmed` / `long.confirmed`) plus the frozen levels
(entry_zone / tp1 / tp2 / stop_loss). Those confirmed ticks ARE the live gate's
historical output. We episode-dedupe them into distinct signals and kline-grade
each with the SAME hold-to-target method as backtest_signals.py, then compare the
confirmed-gate SL rate to the raw-fade baseline (~52%).

Usage:
    PYTHONPATH=hunt python hunt/scripts/gate_edge.py [--gap-hours 3] [--limit 200]
"""

from __future__ import annotations

import argparse
import asyncio
import glob
import json
from datetime import datetime
from typing import Any

from hunt_watch.bootstrap import bootstrap

bootstrap()

import aiohttp

from hunt_watch.paths import DATA

# Reuse the exact grading used for the raw baseline (apples-to-apples).
from importlib import import_module

_bt = import_module("backtest_signals")  # hunt/scripts on path via bootstrap
_fetch_klines = _bt._fetch_klines
_simulate = _bt._simulate

RAW_BASELINE_SL = 0.52  # enriched raw-fade sl_hit rate (n=252)


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--gap-hours", type=float, default=3.0, help="episode dedupe gap per symbol")
    p.add_argument("--limit", type=int, default=200, help="max distinct signals to grade")
    p.add_argument("--tf", default="5m")
    p.add_argument("--window", type=int, default=96)
    p.add_argument("--direction", choices=["short", "long", "both"], default="short")
    return p.parse_args()


def _ts(raw: str) -> datetime | None:
    try:
        return datetime.fromisoformat(str(raw))
    except (TypeError, ValueError):
        return None


def collect_confirmed(direction: str, *, gap_hours: float) -> list[dict[str, Any]]:
    """One signal per symbol per episode (first confirm, then suppress until a gap)."""
    field = "dump" if direction == "short" else "long"
    rows: list[tuple[datetime, str, dict[str, Any]]] = []
    for f in sorted(glob.glob(str(DATA / "dump_minute_watch*.jsonl"))):
        for line in open(f, encoding="utf-8"):
            if not line.strip():
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            d = r.get(field) or {}
            if not (d.get("confirmed") and d.get("tp1") and d.get("stop_loss")):
                continue
            ts = _ts(r.get("ts"))
            if ts is None:
                continue
            rows.append((ts, str(r.get("symbol") or ""), d))
    rows.sort(key=lambda x: x[0])

    last_open: dict[str, datetime] = {}
    out: list[dict[str, Any]] = []
    for ts, sym, d in rows:
        prev = last_open.get(sym)
        if prev is not None and (ts - prev).total_seconds() < gap_hours * 3600:
            continue
        last_open[sym] = ts
        ez = d.get("entry_zone") or []
        out.append(
            {
                "source": "confirmed_tick",
                "symbol": sym,
                "direction": direction,
                "opened_at": ts.isoformat(),
                "entry_lo": ez[0] if len(ez) > 0 else d.get("tp1"),
                "entry_hi": ez[1] if len(ez) > 1 else d.get("tp1"),
                "stop_loss": d.get("stop_loss"),
                "tp1": d.get("tp1"),
                "tp2": d.get("tp2"),
                "fuel": d.get("dump_fuel"),
                "lifecycle_phase": d.get("lifecycle_phase"),
            }
        )
    return out


async def _grade(signals: list[dict[str, Any]], *, tf: str, window: int) -> list[dict[str, Any]]:
    graded: list[dict[str, Any]] = []
    async with aiohttp.ClientSession() as session:
        for sig in signals:
            ts = _ts(sig["opened_at"])
            if ts is None:
                continue
            start_ms = int(ts.timestamp() * 1000)
            candles = await _fetch_klines(session, sig["symbol"], tf, start_ms, window)
            if not candles:
                graded.append({**sig, "bt_outcome": "no_data"})
                continue
            res = _simulate(sig, candles, direction=sig["direction"])
            graded.append({**sig, **{f"bt_{k}": v for k, v in res.items()}})
            await asyncio.sleep(0.08)
    return graded


def _summarize(graded: list[dict[str, Any]], *, direction: str) -> None:
    from collections import Counter

    counts: Counter[str] = Counter(r.get("bt_outcome", "no_data") for r in graded)
    gradeable = sum(counts[k] for k in ("tp1_hit", "tp2_hit", "sl_hit", "timeout"))
    sl = counts.get("sl_hit", 0)
    tp = counts.get("tp1_hit", 0) + counts.get("tp2_hit", 0)
    sl_rate = sl / gradeable if gradeable else 0.0
    tp_rate = tp / gradeable if gradeable else 0.0
    print(f"\n=== GATE EDGE ({direction}) — confirmed setups vs raw baseline ===")
    print(f"  distinct confirmed signals graded: {gradeable}")
    print(f"  outcome mix: {dict(counts)}")
    print(f"  confirmed SL rate = {sl_rate:.0%}  ·  TP1-reach = {tp_rate:.0%}")
    print(f"  RAW baseline SL = {RAW_BASELINE_SL:.0%}")
    delta = RAW_BASELINE_SL - sl_rate
    if gradeable < 20:
        print(f"  ⚠ n={gradeable} thin — directional only")
    if delta > 0.05:
        print(f"  ✅ GATE EDGE CONFIRMED: -{delta*100:.0f}pp SL vs raw (gate filters losers)")
    elif delta < -0.05:
        print(f"  ❌ gate WORSE than raw by {-delta*100:.0f}pp — confirm logic not adding edge")
    else:
        print(f"  ≈ no material edge ({delta*100:+.0f}pp)")


async def _main(args: argparse.Namespace) -> int:
    dirs = ["short", "long"] if args.direction == "both" else [args.direction]
    out_rows: list[dict[str, Any]] = []
    for direction in dirs:
        sigs = collect_confirmed(direction, gap_hours=args.gap_hours)[: args.limit]
        print(f"{direction}: {len(sigs)} distinct confirmed signals (gap={args.gap_hours}h)")
        graded = await _grade(sigs, tf=args.tf, window=args.window)
        _summarize(graded, direction=direction)
        out_rows.extend(graded)
    out_path = DATA / "gate_edge_outcomes.jsonl"
    with out_path.open("w", encoding="utf-8") as fh:
        for r in out_rows:
            fh.write(json.dumps(r, default=str) + "\n")
    print(f"\nWrote {len(out_rows)} rows → {out_path}")
    try:
        from hunt_research.labels import rebuild_unified, slice_stats

        rebuild_unified()
        matrix = slice_stats()
        if matrix:
            print("\n=== H-B phase×direction slices (unified labels) ===")
            for key, vals in sorted(matrix.items()):
                print(f"  {key}: n={vals['n']} sl={vals['sl_rate']} tp1+={vals['tp1_plus_rate']}")
    except ImportError:
        pass
    return 0


def main() -> int:
    return asyncio.run(_main(_parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
