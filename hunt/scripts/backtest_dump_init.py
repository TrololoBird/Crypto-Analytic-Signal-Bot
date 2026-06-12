#!/usr/bin/env python3
"""Backtest dump_init scoring on pump_history dump retraces + ESPORTS JSONL replay.

Walks 1m Polars indicators minute-by-minute and reports when setup/trigger/verdict
would have fired vs the recorded dump retrace event.

Usage:
    PYTHONPATH=hunt python hunt/scripts/backtest_dump_init.py --limit 10
    PYTHONPATH=hunt python hunt/scripts/backtest_dump_init.py --replay-esports
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from hunt_watch.bootstrap import bootstrap

bootstrap()

from engine.features.prepare_frame import _prepare_frame
from hunt_watch.dump_init_score import score_dump_init
from hunt_watch.paths import DATA, PUMP_HISTORY


def _load_watch():
    path = Path(__file__).resolve().parent / "watch.py"
    spec = importlib.util.spec_from_file_location("hunt_watch_script", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules["hunt_watch_script"] = mod
    spec.loader.exec_module(mod)
    return mod


def _tf_snap(hw: Any, df: Any) -> dict[str, Any]:
    if df is None or df.is_empty():
        return {"status": "empty"}
    work = _prepare_frame(df)
    if work is None or work.is_empty():
        return hw._tf_snapshot_lite(df)
    snap = hw._tf_snapshot(work)
    closed = hw._tf_snapshot(work, closed=True)
    return {
        **{k: snap.get(k) for k in (
            "close", "rsi14", "adx14", "atr14", "macd_hist", "macd_line", "macd_signal",
            "bb_width_pct", "ema20", "ema50", "ema200", "vwap", "delta_ratio",
            "donchian_width_pct", "structure", "bias",
        )},
        "closed_rsi14": closed.get("rsi14"),
        "closed_macd_hist": closed.get("macd_hist"),
        "closed_close": closed.get("close"),
        "bars": int(work.height),
    }


def _ot_ms(df: Any, i: int) -> int:
    v = df["open_time"][i]
    if hasattr(v, "timestamp"):
        return int(v.timestamp() * 1000)
    return int(v)


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--limit", type=int, default=10, help="max pump_history dump events")
    p.add_argument("--replay-esports", action="store_true", help="replay deep_watch JSONL")
    p.add_argument("--lookback-min", type=int, default=45, help="minutes before event")
    return p.parse_args()


def _dump_events(limit: int) -> list[dict[str, Any]]:
    if not PUMP_HISTORY.exists():
        return []
    raw = json.loads(PUMP_HISTORY.read_text(encoding="utf-8"))
    events = [
        e for e in (raw.get("event_log") or [])
        if e.get("kind") == "dump"
        and e.get("type") == "retrace_hit"
        and str(e.get("symbol") or "").isascii()
    ]
    events.sort(key=lambda e: e.get("ts") or "", reverse=True)
    return events[:limit]


async def _walk_symbol_event(
    client: HuntCcxtClient,
    hw: Any,
    *,
    symbol: str,
    event_dt: datetime,
    lookback_min: int,
) -> dict[str, Any]:
    start = event_dt - timedelta(minutes=lookback_min)
    end = event_dt + timedelta(minutes=15)

    k1 = await client.fetch_klines(symbol, "1m", limit=500)
    k5 = await client.fetch_klines(symbol, "5m", limit=500)
    k15 = await client.fetch_klines(symbol, "15m", limit=500)
    k1h = await client.fetch_klines(symbol, "1h", limit=500)

    w1 = _prepare_frame(k1)
    if w1.is_empty():
        return {"symbol": symbol, "status": "no_data"}

    event_ms = int(event_dt.timestamp() * 1000)
    start_ms = int(start.timestamp() * 1000)
    end_ms = int(end.timestamp() * 1000)
    first_armed: str | None = None
    first_likely: str | None = None
    first_trigger: str | None = None
    prev_record: dict[str, Any] | None = None
    peak_price = 0.0
    armed_idx: int | None = None
    armed_price = 0.0
    armed_score = 0

    for i in range(w1.height):
        ms = _ot_ms(w1, i)
        if ms < start_ms or ms > end_ms:
            continue
        close = float(w1["close"][i])
        peak_price = max(peak_price, float(w1["high"][i]))
        fall = (peak_price - close) / peak_price * 100.0 if peak_price > 0 else 0.0
        phase = "exhaustion_at_high" if fall < 3 else ("dump_active" if fall >= 8 else "distribution")

        w1_slice = w1.head(i + 1)
        tf = {
            "1m": _tf_snap(hw, w1_slice),
            "5m": _tf_snap(hw, k5),
            "15m": _tf_snap(hw, k15),
            "1h": _tf_snap(hw, k1h),
        }
        row = {
            "price": close,
            "dump": {"support_break_level": peak_price * 0.985},
            "lifecycle": {"phase": phase, "fall_from_high_pct": round(fall, 2)},
            "market": {},
        }
        score, reasons, verdict = score_dump_init(row=row, micro={}, tf=tf, prev=prev_record)
        t = datetime.fromtimestamp(ms / 1000, tz=UTC).strftime("%H:%M")
        if first_trigger is None and any(
            r.startswith(("1m_macd", "below_support", "fall_trigger")) for r in reasons
        ):
            first_trigger = t
        if first_armed is None and verdict in ("DUMP_ARMED", "DUMP_LIKELY"):
            first_armed = f"{t} score={score}"
            armed_idx = i
            armed_price = close
            armed_score = score
        if first_likely is None and verdict == "DUMP_LIKELY":
            first_likely = f"{t} score={score}"
        prev_record = {"price": close, "lifecycle": row["lifecycle"], "timeframes": tf}

    # --- Outcome grading: simulate a fade-short from the armed bar (R5 validation) ---
    bt_outcome = "not_armed"
    if armed_idx is not None and armed_price > 0:
        # ATR% from the 14 1m bars preceding the arming bar
        lo_i = max(0, armed_idx - 14)
        trs: list[float] = []
        prev_c = float(w1["close"][lo_i])
        for j in range(lo_i + 1, armed_idx + 1):
            hi, lo, cl = float(w1["high"][j]), float(w1["low"][j]), float(w1["close"][j])
            trs.append(max(hi - lo, abs(hi - prev_c), abs(lo - prev_c)))
            prev_c = cl
        atr_pct = (sum(trs) / len(trs) / armed_price * 100.0) if trs else 1.0
        atr_pct = max(0.3, min(atr_pct, 8.0))
        sl = armed_price * (1.0 + 1.5 * atr_pct / 100.0)
        tp1 = armed_price * (1.0 - 1.2 * atr_pct / 100.0)
        bt_outcome = "timeout"
        for j in range(armed_idx + 1, min(armed_idx + 1 + 96, w1.height)):
            hi, lo = float(w1["high"][j]), float(w1["low"][j])
            if hi >= sl:
                bt_outcome = "sl_hit"
                break
            if lo <= tp1:
                bt_outcome = "tp1_hit"
                break

    event_t = event_dt.strftime("%H:%M")
    return {
        "symbol": symbol,
        "event_utc": event_dt.isoformat(),
        "event_time": event_t,
        "first_trigger": first_trigger,
        "first_armed": first_armed,
        "first_likely": first_likely,
        "armed_score": armed_score,
        "bt_outcome": bt_outcome,
        "peak": round(peak_price, 6),
    }


def _replay_esports_jsonl() -> list[dict[str, Any]]:
    path = DATA / "deep_watch_ESPORTSUSDT.jsonl"
    if not path.exists():
        return [{"status": "missing_jsonl"}]
    rows = [json.loads(l) for l in path.read_text().strip().split("\n") if l.strip()]
    prev: dict[str, Any] | None = None
    out: list[dict[str, Any]] = []
    for r in rows:
        tf = r.get("timeframes") or {}
        micro = r.get("microstructure") or {}
        dump_hs = r.get("hunt_short") or {}
        row = {
            "price": r.get("price"),
            "dump": {
                "support_break_level": dump_hs.get("support_break"),
                "dump_score": dump_hs.get("score"),
                "confirmed": dump_hs.get("confirmed"),
            },
            "lifecycle": r.get("lifecycle") or {},
            "market": r.get("market") or {},
        }
        score, reasons, verdict = score_dump_init(row=row, micro=micro, tf=tf, prev=prev)
        out.append({
            "ts": r["ts"][11:19],
            "price": r.get("price"),
            "score": score,
            "verdict": verdict,
            "triggers": [x for x in reasons if "macd" in x or x.startswith("fall")],
            "setup": [x for x in reasons if x.startswith(("1h_", "15m_", "top_ls", "phase", "funding"))],
        })
        prev = {"price": r.get("price"), "lifecycle": r.get("lifecycle"), "timeframes": tf}
    return out


async def _main_async(args: argparse.Namespace) -> int:
    if args.replay_esports:
        replay = _replay_esports_jsonl()
        print("=== ESPORTS deep_watch replay (new scoring) ===")
        for row in replay:
            if row.get("status"):
                print(row)
                continue
            if row["ts"] < "22:54" or row["ts"] > "23:12":
                continue
            print(
                f"{row['ts']} px={float(row['price']):.5f} score={row['score']:3d} "
                f"{row['verdict']:12s} trig={row['triggers']}"
            )
        armed = [r for r in replay if r.get("verdict") in ("DUMP_ARMED", "DUMP_LIKELY")]
        print(f"\narmed/likely snapshots: {len(armed)}")
        if armed:
            print(f"first: {armed[0]['ts']} {armed[0]['verdict']} score={armed[0]['score']}")
        return 0

    events = _dump_events(args.limit)
    if not events:
        print("no pump_history dump retrace events")
        return 1

    hw = _load_watch()
    client = HuntCcxtClient()
    results: list[dict[str, Any]] = []
    for ev in events:
        sym = str(ev.get("symbol") or "")
        ts_raw = ev.get("ts") or ""
        try:
            event_dt = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
        except ValueError:
            continue
        if event_dt.tzinfo is None:
            event_dt = event_dt.replace(tzinfo=UTC)
        print(f"backtesting {sym} @ {event_dt.strftime('%Y-%m-%d %H:%M')} UTC ...")
        try:
            results.append(
                await _walk_symbol_event(
                    client, hw, symbol=sym, event_dt=event_dt, lookback_min=args.lookback_min
                )
            )
        except (OSError, ValueError, RuntimeError) as exc:
            results.append({"symbol": sym, "status": "error", "error": str(exc)})
        await asyncio.sleep(0.15)

    await client.close()

    print("\n=== BACKTEST SUMMARY ===")
    print(f"{'symbol':12s} {'event':6s} {'trigger':8s} {'armed':20s} {'likely':20s}")
    ok = 0
    for r in results:
        if r.get("status"):
            print(f"{r.get('symbol','?'):12s} SKIP {r.get('status')}")
            continue
        ok += 1
        print(
            f"{r['symbol']:12s} {r['event_time']:6s} "
            f"{r.get('first_trigger') or '-':8s} "
            f"{r.get('first_armed') or '-':20s} "
            f"{r.get('first_likely') or '-':20s}"
        )
    print(f"\ngraded: {ok}/{len(results)}")

    # --- R5 validation: does armed dump_init beat the 52% raw-fade SL baseline? ---
    armed = [r for r in results if r.get("bt_outcome") in ("tp1_hit", "sl_hit", "timeout")]
    if armed:
        tp1 = sum(1 for r in armed if r["bt_outcome"] == "tp1_hit")
        slh = sum(1 for r in armed if r["bt_outcome"] == "sl_hit")
        tmo = sum(1 for r in armed if r["bt_outcome"] == "timeout")
        n = len(armed)
        print("\n=== R5 VALIDATION (armed → fade-short outcome) ===")
        print(f"  armed n={n}: tp1_hit={tp1} sl_hit={slh} timeout={tmo}")
        print(f"  armed SL rate={slh / n:.0%}  (raw-fade baseline=52%)")
        verdict = (
            "PASS — armed SL beats baseline; candidate for live secondary factor"
            if slh / n < 0.45
            else "FAIL — no edge over raw fade; DO NOT wire live yet"
        )
        print(f"  verdict: {verdict}")

    out_path = DATA / "backtest_dump_init.jsonl"
    with out_path.open("w", encoding="utf-8") as fh:
        for r in results:
            fh.write(json.dumps(r, default=str) + "\n")
    print(f"wrote {out_path}")
    return 0


def main() -> None:
    args = _parse_args()
    raise SystemExit(asyncio.run(_main_async(args)))


if __name__ == "__main__":
    main()
