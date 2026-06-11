#!/usr/bin/env python3
"""Backtest closed hunt signals against historical klines.

For each closed signal in signal_history.jsonl, fetches klines starting at
opened_at and walks candles to find what would have happened if we had held
until TP1/TP2/SL (ignoring lifecycle exits).

Outcome classes:
  tp2_hit    — price reached TP2 (best outcome)
  tp1_hit    — price reached TP1 but not TP2
  sl_hit     — SL hit before TP1
  timeout    — neither TP1 nor SL in window
  no_data    — insufficient klines to judge

Usage:
    python hunt/scripts/backtest_signals.py [--limit N] [--tf 5m] [--window 48]
"""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import datetime, timezone
from typing import Any

from hunt_watch.bootstrap import bootstrap

bootstrap()

import aiohttp

from hunt_watch.paths import SIGNAL_HISTORY


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--limit", type=int, default=100, help="max signals to backtest")
    p.add_argument("--tf", default="5m", help="candle timeframe (default: 5m)")
    p.add_argument("--window", type=int, default=72, help="candles to look ahead")
    p.add_argument("--symbol", default="", help="filter to one symbol")
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

    mae = 0.0  # max adverse excursion %
    mfe = 0.0  # max favorable excursion %
    candles_to_tp1 = candles_to_sl = None

    for i, c in enumerate(candles):
        lo = float(c[3])
        hi = float(c[2])
        if direction == "short":
            # Favorable = down, adverse = up
            if entry_mid > 0:
                cur_mfe = max(0.0, (entry_mid - lo) / entry_mid * 100.0)
                cur_mae = max(0.0, (hi - entry_mid) / entry_mid * 100.0)
                mfe = max(mfe, cur_mfe)
                mae = max(mae, cur_mae)
            # SL = price goes ABOVE sl
            if hi >= sl and candles_to_sl is None:
                candles_to_sl = i + 1
            # TP checks (need price to go below)
            if lo <= tp1 and candles_to_tp1 is None:
                candles_to_tp1 = i + 1
            if lo <= tp2 and candles_to_tp1 is not None:
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
        else:  # long
            if entry_mid > 0:
                cur_mfe = max(0.0, (hi - entry_mid) / entry_mid * 100.0)
                cur_mae = max(0.0, (entry_mid - lo) / entry_mid * 100.0)
                mfe = max(mfe, cur_mfe)
                mae = max(mae, cur_mae)
            if lo <= sl and candles_to_sl is None:
                candles_to_sl = i + 1
            if hi >= tp1 and candles_to_tp1 is None:
                candles_to_tp1 = i + 1
            if hi >= tp2 and candles_to_tp1 is not None:
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


async def _run(args: argparse.Namespace) -> None:
    if not SIGNAL_HISTORY.exists():
        print("No signal_history.jsonl yet — run watch until signals close.")
        return

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
            signals.append(r)
    signals = signals[-args.limit :]

    print(f"Backtesting {len(signals)} closed signals (tf={args.tf}, window={args.window} candles)\n")

    tf_ms = _tf_to_ms(args.tf)
    outcomes: dict[str, int] = {}
    rows: list[dict[str, Any]] = []

    async with aiohttp.ClientSession() as session:
        for sig in signals:
            sym = sig.get("symbol", "?")
            direction = sig.get("direction", "short")
            try:
                opened_dt = datetime.fromisoformat(str(sig["opened_at"]))
                start_ms = int(opened_dt.timestamp() * 1000)
            except (TypeError, ValueError):
                rows.append({**sig, "bt_outcome": "no_data", "bt_reason": "bad_opened_at"})
                continue
            candles = await _fetch_klines(session, sym, args.tf, start_ms, args.window)
            if not candles:
                rows.append({**sig, "bt_outcome": "no_data", "bt_reason": "no_klines"})
                continue
            result = _simulate(sig, candles, direction=direction)
            outcome = result.get("outcome", "no_data")
            outcomes[outcome] = outcomes.get(outcome, 0) + 1
            rows.append({**sig, **{f"bt_{k}": v for k, v in result.items()}})
            tp1 = float(sig.get("tp1") or 0)
            entry_lo = float(sig.get("entry_lo") or 0)
            entry_hi = float(sig.get("entry_hi") or 0)
            actual_close = sig.get("close_reason", "?")
            actual_pnl = sig.get("pnl_pct")
            c2tp = result.get("candles_to_tp1", "-")
            c2sl = result.get("candles_to_sl", "-")
            print(
                f"{sym:12s} {direction:5s} | actual={actual_close:20s} pnl={str(actual_pnl):6s}% "
                f"| bt={outcome:10s} c2tp1={c2tp} c2sl={c2sl} "
                f"mfe={result.get('mfe_pct','?')}% mae={result.get('mae_pct','?')}%"
            )
            await asyncio.sleep(0.1)  # rate limit

    print(f"\n=== BACKTEST SUMMARY (n={len(rows)}) ===")
    for o, n in sorted(outcomes.items(), key=lambda kv: -kv[1]):
        pct = n / len(rows) * 100 if rows else 0
        print(f"  {o:12s} {n:3d}  ({pct:.0f}%)")

    # MFE analysis
    bt_mfes = [r.get("bt_mfe_pct") for r in rows if isinstance(r.get("bt_mfe_pct"), (int, float))]
    if bt_mfes:
        bt_mfes.sort()
        n = len(bt_mfes)
        print(f"\n  MFE distribution (n={n}):")
        print(f"    p25={bt_mfes[n//4]:.1f}%  median={bt_mfes[n//2]:.1f}%  p75={bt_mfes[n*3//4]:.1f}%  max={bt_mfes[-1]:.1f}%")

    # TP1 gap: did actual exits miss a TP1 that backtest says would have hit?
    missed_tp1 = [
        r for r in rows
        if r.get("bt_outcome") in ("tp1_hit", "tp2_hit")
        and r.get("close_reason") not in ("tp1", "tp2")
    ]
    if missed_tp1:
        print(f"\n  ⚠ Signals that would have hit TP1 but were closed early: {len(missed_tp1)}")
        for r in missed_tp1:
            print(f"    {r.get('symbol')} {r.get('direction')} closed={r.get('close_reason')} bt={r.get('bt_outcome')} c2tp1={r.get('bt_candles_to_tp1')}")


def main() -> int:
    args = _parse_args()
    asyncio.run(_run(args))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
