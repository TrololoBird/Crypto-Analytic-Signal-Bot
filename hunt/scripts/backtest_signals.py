#!/usr/bin/env python3
"""Backtest closed hunt signals against historical klines.

For each closed signal in signal_history.jsonl (and optionally pump/dump leg
events from pump_history.json), fetches klines starting at opened_at and walks
candles to find what would have happened if we had held until TP1/TP2/SL.

Outcome classes:
  tp2_hit    — price reached TP2 (best outcome)
  tp1_hit    — price reached TP1 but not TP2
  sl_hit     — SL hit before TP1
  timeout    — neither TP1 nor SL in window
  no_data    — insufficient klines to judge

Usage:
    python hunt/scripts/backtest_signals.py [--limit N] [--tf 5m] [--window 48]
    python hunt/scripts/backtest_signals.py --include-pump-events --limit 50
"""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from hunt_watch.bootstrap import bootstrap

bootstrap()

import aiohttp

from hunt_watch.backtest_synthetic import atr_levels, atr_pct_from_klines, leg_events_to_signals
from hunt_watch.paths import DATA, SIGNAL_HISTORY


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--limit", type=int, default=100, help="max signals per source")
    p.add_argument("--tf", default="5m", help="candle timeframe (default: 5m)")
    p.add_argument("--window", type=int, default=72, help="candles to look ahead")
    p.add_argument("--symbol", default="", help="filter to one symbol")
    p.add_argument(
        "--include-pump-events",
        action="store_true",
        help="also backtest synthetic signals from pump_history leg events",
    )
    p.add_argument(
        "--pump-only",
        action="store_true",
        help="backtest pump_history legs only (skip signal_history.jsonl)",
    )
    p.add_argument(
        "--out",
        default="",
        help="write graded rows to JSONL (default: hunt/data/backtest_outcomes.jsonl)",
    )
    p.add_argument(
        "--enrich",
        action="store_true",
        help="refine synthetic leg levels from real pre-leg ATR (REST enrichment)",
    )
    return p.parse_args()


def _tf_to_ms(tf: str) -> int:
    units = {"m": 60_000, "h": 3_600_000, "d": 86_400_000}
    return int(tf[:-1]) * units[tf[-1]]


async def _fetch_klines(
    session: aiohttp.ClientSession,
    symbol: str,
    tf: str,
    start_ms: int,
    limit: int,
) -> list[list[Any]]:
    url = "https://fapi.binance.com/fapi/v1/klines"
    params = {"symbol": symbol, "interval": tf, "startTime": start_ms, "limit": limit}
    async with session.get(url, params=params) as r:
        if r.status != 200:
            return []
        return await r.json()


def _simulate(
    signal: dict[str, Any],
    candles: list[list[Any]],
    *,
    direction: str,
) -> dict[str, Any]:
    """Walk candles and find first TP/SL touch. Returns outcome dict."""
    tp1 = float(signal.get("tp1") or 0)
    tp2 = float(signal.get("tp2") or 0)
    sl = float(signal.get("stop_loss") or 0)
    if tp1 <= 0 or sl <= 0:
        return {"outcome": "no_data", "reason": "missing_levels"}

    entry_lo = float(signal.get("entry_lo") or 0)
    entry_hi = float(signal.get("entry_hi") or 0)
    entry_mid = (entry_lo + entry_hi) / 2.0 if entry_lo and entry_hi else entry_lo or entry_hi

    mae = 0.0
    mfe = 0.0
    candles_to_tp1 = candles_to_sl = None

    for i, c in enumerate(candles):
        lo = float(c[3])
        hi = float(c[2])
        if direction == "short":
            if entry_mid > 0:
                cur_mfe = max(0.0, (entry_mid - lo) / entry_mid * 100.0)
                cur_mae = max(0.0, (hi - entry_mid) / entry_mid * 100.0)
                mfe = max(mfe, cur_mfe)
                mae = max(mae, cur_mae)
            if hi >= sl and candles_to_sl is None:
                candles_to_sl = i + 1
            if lo <= tp1 and candles_to_tp1 is None:
                candles_to_tp1 = i + 1
            if tp2 > 0 and lo <= tp2 and candles_to_tp1 is not None:
                return {
                    "outcome": "tp2_hit",
                    "candles_to_tp1": candles_to_tp1,
                    "candles_to_tp2": i + 1,
                    "mfe_pct": round(mfe, 2),
                    "mae_pct": round(mae, 2),
                }
            if candles_to_sl and (candles_to_tp1 is None or candles_to_sl <= candles_to_tp1):
                return {
                    "outcome": "sl_hit",
                    "candles_to_sl": candles_to_sl,
                    "mfe_pct": round(mfe, 2),
                    "mae_pct": round(mae, 2),
                }
        else:
            if entry_mid > 0:
                cur_mfe = max(0.0, (hi - entry_mid) / entry_mid * 100.0)
                cur_mae = max(0.0, (entry_mid - lo) / entry_mid * 100.0)
                mfe = max(mfe, cur_mfe)
                mae = max(mae, cur_mae)
            if lo <= sl and candles_to_sl is None:
                candles_to_sl = i + 1
            if hi >= tp1 and candles_to_tp1 is None:
                candles_to_tp1 = i + 1
            if tp2 > 0 and hi >= tp2 and candles_to_tp1 is not None:
                return {
                    "outcome": "tp2_hit",
                    "candles_to_tp1": candles_to_tp1,
                    "candles_to_tp2": i + 1,
                    "mfe_pct": round(mfe, 2),
                    "mae_pct": round(mae, 2),
                }
            if candles_to_sl and (candles_to_tp1 is None or candles_to_sl <= candles_to_tp1):
                return {
                    "outcome": "sl_hit",
                    "candles_to_sl": candles_to_sl,
                    "mfe_pct": round(mfe, 2),
                    "mae_pct": round(mae, 2),
                }

    if candles_to_tp1 is not None:
        return {
            "outcome": "tp1_hit",
            "candles_to_tp1": candles_to_tp1,
            "mfe_pct": round(mfe, 2),
            "mae_pct": round(mae, 2),
        }
    return {"outcome": "timeout", "mfe_pct": round(mfe, 2), "mae_pct": round(mae, 2)}


def _load_live_signals(args: argparse.Namespace) -> list[dict[str, Any]]:
    if not SIGNAL_HISTORY.exists():
        return []
    signals: list[dict[str, Any]] = []
    for line in SIGNAL_HISTORY.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        if args.symbol and r.get("symbol") != args.symbol:
            continue
        if r.get("opened_at") and r.get("tp1") and r.get("stop_loss"):
            r.setdefault("source", "signal_history")
            if not r.get("lifecycle_phase"):
                r["lifecycle_phase"] = r.get("entry_lifecycle_phase")
            signals.append(r)
    return signals[-args.limit :]


def _collect_signals(args: argparse.Namespace) -> list[dict[str, Any]]:
    live = [] if args.pump_only else _load_live_signals(args)
    synthetic: list[dict[str, Any]] = []
    if args.include_pump_events or args.pump_only:
        synthetic = leg_events_to_signals(limit=args.limit)
        if args.symbol:
            synthetic = [s for s in synthetic if s.get("symbol") == args.symbol]
    return live + synthetic


async def _enrich_levels(
    session: aiohttp.ClientSession,
    sig: dict[str, Any],
    *,
    tf: str,
    start_ms: int,
) -> dict[str, Any]:
    """Refine synthetic levels from real pre-leg ATR (REST enrichment, R3)."""
    sym = sig.get("symbol", "?")
    leg = "dump" if sig.get("leg_kind") == "dump" else "pump"
    tf_ms = _tf_to_ms(tf)
    pre_start = start_ms - tf_ms * 30  # ~30 candles before the leg
    pre = await _fetch_klines(session, sym, tf, pre_start, 30)
    atr_pct = atr_pct_from_klines(pre) if pre else None
    if atr_pct is None:
        return sig
    price = float(sig.get("entry_hi") if leg == "pump" else sig.get("entry_lo") or 0)
    lv = atr_levels(leg, price, atr_pct)
    if not lv.get("tp1"):
        return sig
    enriched = dict(sig)
    enriched.update(
        {
            "entry_lo": lv["entry_lo"],
            "entry_hi": lv["entry_hi"],
            "stop_loss": lv["stop_loss"],
            "tp1": lv["tp1"],
            "tp2": lv["tp2"],
            "atr_pct": lv["atr_pct"],
            "levels_source": "atr_enriched",
        }
    )
    return enriched


async def _backtest_one(
    session: aiohttp.ClientSession,
    sig: dict[str, Any],
    *,
    tf: str,
    window: int,
    enrich: bool = False,
) -> dict[str, Any]:
    sym = sig.get("symbol", "?")
    direction = sig.get("direction", "short")
    try:
        opened_dt = datetime.fromisoformat(str(sig["opened_at"]))
        start_ms = int(opened_dt.timestamp() * 1000)
    except (TypeError, ValueError):
        return {**sig, "bt_outcome": "no_data", "bt_reason": "bad_opened_at"}
    # Enrich only synthetic legs (live signals already carry real tracker levels)
    if enrich and sig.get("source") == "pump_history":
        sig = await _enrich_levels(session, sig, tf=tf, start_ms=start_ms)
    candles = await _fetch_klines(session, sym, tf, start_ms, window)
    if not candles:
        return {**sig, "bt_outcome": "no_data", "bt_reason": "no_klines"}
    result = _simulate(sig, candles, direction=direction)
    row = {**sig, **{f"bt_{k}": v for k, v in result.items()}}
    if not row.get("lifecycle_phase"):
        row["lifecycle_phase"] = row.get("entry_lifecycle_phase")
    return row


def _print_row(sig: dict[str, Any], result: dict[str, Any]) -> None:
    sym = sig.get("symbol", "?")
    direction = sig.get("direction", "short")
    source = sig.get("source", "?")
    outcome = result.get("bt_outcome", result.get("outcome", "no_data"))
    actual_close = sig.get("close_reason", "—")
    actual_pnl = sig.get("pnl_pct", "—")
    c2tp = result.get("bt_candles_to_tp1", result.get("candles_to_tp1", "-"))
    c2sl = result.get("bt_candles_to_sl", result.get("candles_to_sl", "-"))
    print(
        f"{sym:12s} {direction:5s} [{source:14s}] | actual={str(actual_close):20s} "
        f"pnl={str(actual_pnl):6s} | bt={outcome:10s} c2tp1={c2tp} c2sl={c2sl} "
        f"mfe={result.get('bt_mfe_pct', result.get('mfe_pct', '?'))}% "
        f"mae={result.get('bt_mae_pct', result.get('mae_pct', '?'))}%"
    )


async def _run(args: argparse.Namespace) -> None:
    signals = _collect_signals(args)
    if not signals:
        print("No signals to backtest — run watch or pass --include-pump-events.")
        return

    live_n = sum(1 for s in signals if s.get("source") == "signal_history")
    synth_n = len(signals) - live_n
    print(
        f"Backtesting {len(signals)} signals "
        f"(live={live_n} synthetic={synth_n}, tf={args.tf}, window={args.window} candles)\n"
    )

    outcomes: dict[str, int] = {}
    rows: list[dict[str, Any]] = []

    async with aiohttp.ClientSession() as session:
        for sig in signals:
            row = await _backtest_one(
                session, sig, tf=args.tf, window=args.window, enrich=args.enrich
            )
            outcome = row.get("bt_outcome", "no_data")
            outcomes[outcome] = outcomes.get(outcome, 0) + 1
            rows.append(row)
            _print_row(sig, row)
            await asyncio.sleep(0.08)

    graded = [r for r in rows if r.get("bt_outcome") not in ("no_data", None)]
    print(f"\n=== BACKTEST SUMMARY (n={len(rows)}, graded={len(graded)}) ===")
    for o, n in sorted(outcomes.items(), key=lambda kv: -kv[1]):
        pct = n / len(rows) * 100 if rows else 0
        print(f"  {o:12s} {n:3d}  ({pct:.0f}%)")

    bt_mfes = [r.get("bt_mfe_pct") for r in graded if isinstance(r.get("bt_mfe_pct"), (int, float))]
    if bt_mfes:
        bt_mfes.sort()
        n = len(bt_mfes)
        print(f"\n  MFE distribution (graded n={n}):")
        print(
            f"    p25={bt_mfes[n//4]:.1f}%  median={bt_mfes[n//2]:.1f}%  "
            f"p75={bt_mfes[n*3//4]:.1f}%  max={bt_mfes[-1]:.1f}%"
        )

    missed_tp1 = [
        r
        for r in rows
        if r.get("source") == "signal_history"
        and r.get("bt_outcome") in ("tp1_hit", "tp2_hit")
        and r.get("close_reason") not in ("tp1", "tp2")
    ]
    if missed_tp1:
        print(f"\n  ⚠ Live signals closed early but BT would hit TP1: {len(missed_tp1)}")
        for r in missed_tp1:
            print(
                f"    {r.get('symbol')} {r.get('direction')} closed={r.get('close_reason')} "
                f"bt={r.get('bt_outcome')} c2tp1={r.get('bt_candles_to_tp1')}"
            )

    out_path = Path(args.out) if args.out else DATA / "backtest_outcomes.jsonl"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, default=str) + "\n")
    print(f"\nWrote {len(rows)} rows → {out_path}")


def main() -> int:
    args = _parse_args()
    asyncio.run(_run(args))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
