#!/usr/bin/env python3
"""One-shot reconciliation of hunt tracker state against Binance klines.

For every ACTIVE signal, fetch 5m klines since opened_at (public endpoint),
apply intrabar extremes through the same latched SL/TP state machine the
watcher uses, and persist outcomes (close_reason / exit_price / pnl_pct).

Usage:
    python hunt/scripts/reconcile_signals.py [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import urllib.parse
import urllib.request
from datetime import UTC, datetime

from hunt_watch.bootstrap import bootstrap

bootstrap()

from hunt_watch.signal_tracker import (
    load_tracker_state,
    reconcile_signal,
    save_tracker_state,
)

FAPI = "https://fapi.binance.com/fapi/v1/klines"


def _fetch_klines(symbol: str, start_ms: int) -> list[list]:
    out: list[list] = []
    cursor = start_ms
    for _ in range(10):  # max 10 pages x 1500 bars
        params = urllib.parse.urlencode(
            {"symbol": symbol, "interval": "5m", "startTime": cursor, "limit": 1500}
        )
        with urllib.request.urlopen(f"{FAPI}?{params}", timeout=15) as resp:
            batch = json.load(resp)
        if not batch:
            break
        out.extend(batch)
        if len(batch) < 1500:
            break
        cursor = int(batch[-1][0]) + 1
    return out


def _infer_close_from_klines(
    sig: dict,
    *,
    direction: str,
    hi: float,
    lo: float,
    last_price: float,
) -> tuple[str, float]:
    """Best-effort reason/exit for legacy closed rows missing close_reason."""
    stop = float(sig.get("stop_loss") or 0)
    tp1 = float(sig.get("tp1") or 0)
    tp2 = float(sig.get("tp2") or 0)
    if direction == "short":
        if stop > 0 and hi >= stop:
            return "stop_hit", stop
        if tp2 > 0 and lo <= tp2:
            return "tp2", tp2
        if tp1 > 0 and lo <= tp1:
            return "tp1", tp1
    else:
        if stop > 0 and lo <= stop:
            return "stop_hit", stop
        if tp2 > 0 and hi >= tp2:
            return "tp2", tp2
        if tp1 > 0 and hi >= tp1:
            return "tp1", tp1
    return "legacy_unknown", last_price


def backfill_legacy_closed(state: dict, *, now: datetime) -> int:
    """Fill close_reason / exit_price / pnl_pct on closed signals that lack them."""
    n = 0
    signals = state.get("signals") or {}
    for key, sig in signals.items():
        if not isinstance(sig, dict) or sig.get("status") != "closed":
            continue
        existing = str(sig.get("close_reason") or "")
        if existing and existing != "legacy_unknown":
            continue
        symbol, _, direction = key.partition(":")
        try:
            opened = datetime.fromisoformat(str(sig.get("opened_at")))
        except (TypeError, ValueError):
            continue
        end_raw = sig.get("closed_at")
        try:
            end = datetime.fromisoformat(str(end_raw)) if end_raw else now
        except (TypeError, ValueError):
            end = now
        try:
            kl = _fetch_klines(symbol, int(opened.timestamp() * 1000))
        except OSError as exc:
            print(f"{key}: backfill fetch failed: {exc!r}")
            continue
        if not kl:
            continue
        end_ms = int(end.timestamp() * 1000)
        window = [k for k in kl if int(k[0]) <= end_ms] or kl
        hi = max(float(k[2]) for k in window)
        lo = min(float(k[3]) for k in window)
        last_price = float(window[-1][4])
        reason, exit_px = _infer_close_from_klines(
            sig, direction=direction, hi=hi, lo=lo, last_price=last_price
        )
        sig["close_reason"] = reason
        sig["exit_price"] = exit_px
        lo_e = float(sig.get("entry_lo") or 0)
        hi_e = float(sig.get("entry_hi") or 0)
        mid = (lo_e + hi_e) / 2.0 if lo_e > 0 and hi_e > 0 else (lo_e or hi_e)
        if mid > 0:
            raw = (exit_px - mid) / mid * 100.0
            sig["pnl_pct"] = round(raw if direction == "long" else -raw, 2)
        print(f"{key}: backfilled reason={reason} exit={exit_px:g} pnl={sig.get('pnl_pct')}")
        n += 1
    return n


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--backfill-legacy",
        action="store_true",
        help="Infer close_reason/pnl for closed signals missing outcomes",
    )
    args = parser.parse_args()

    state = load_tracker_state()
    now = datetime.now(UTC)
    if args.backfill_legacy:
        filled = backfill_legacy_closed(state, now=now)
        print(f"backfilled {filled} legacy closed")
        if not args.dry_run:
            save_tracker_state(state)
        return 0

    signals = state.get("signals") or {}
    actives = {k: v for k, v in signals.items() if isinstance(v, dict) and v.get("status") == "active"}
    print(f"active signals: {len(actives)}")
    for key, sig in actives.items():
        symbol, _, direction = key.partition(":")
        try:
            opened = datetime.fromisoformat(str(sig.get("opened_at")))
        except (TypeError, ValueError):
            print(f"{key}: bad opened_at, skip")
            continue
        try:
            kl = _fetch_klines(symbol, int(opened.timestamp() * 1000))
        except OSError as exc:
            print(f"{key}: fetch failed: {exc!r}")
            continue
        if not kl:
            print(f"{key}: no klines")
            continue
        hi = max(float(k[2]) for k in kl)
        lo = min(float(k[3]) for k in kl)
        last_price = float(kl[-1][4])
        events = reconcile_signal(
            state, symbol=symbol, direction=direction,
            hi=hi, lo=lo, last_price=last_price, ts=now,
        )
        status = sig.get("status")
        reason = sig.get("close_reason")
        pnl = sig.get("pnl_pct")
        flags = [e.event for e in events]
        print(
            f"{key:24s} hi={hi:g} lo={lo:g} last={last_price:g} -> "
            f"status={status} reason={reason} pnl={pnl} events={flags}"
        )
    if args.dry_run:
        print("dry-run: state NOT saved")
    else:
        save_tracker_state(state)
        print("state saved")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
