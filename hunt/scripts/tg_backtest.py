#!/usr/bin/env python3
"""Backtest every Hunt Telegram send (early prep/start + confirmed entries).

Independent: Binance 5m klines from send timestamp, no trust in tracker PnL.
Writes JSON summary to hunt/data/session/tg_backtest_report.json.

Usage:
    .venv/bin/python hunt/scripts/tg_backtest.py
    .venv/bin/python hunt/scripts/tg_backtest.py --hours 12
"""

from __future__ import annotations

import argparse
import json
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from hunt_watch.bootstrap import bootstrap

bootstrap()

from hunt_watch.param_store import stats_thresholds
from hunt_watch.paths import DATA, SIGNAL_EVENTS, SIGNAL_STATE, TELEGRAM_COOLDOWN

FAPI = "https://fapi.binance.com/fapi/v1/klines"
REPORT_PATH = DATA / "session" / "tg_backtest_report.json"


def _parse_ts(raw: str) -> datetime | None:
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None


def _load_ticks() -> dict[str, list[dict[str, Any]]]:
    by_sym: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for path in sorted(DATA.glob("dump_minute_watch*.jsonl")):
        if path.name == "dump_minute_watch.jsonl" and path.stat().st_size == 0:
            continue
        for line in path.read_text(encoding="utf-8").splitlines()[-12_000:]:
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            sym = row.get("symbol")
            if sym and not row.get("error"):
                by_sym[str(sym).upper()].append(row)
    return by_sym


def _nearest_tick(
    ticks: dict[str, list[dict[str, Any]]], symbol: str, ts: datetime
) -> tuple[dict[str, Any] | None, float | None]:
    rows = ticks.get(symbol.upper()) or []
    best: dict[str, Any] | None = None
    best_s: float | None = None
    for row in rows:
        rt = row.get("ts") or row.get("timestamp")
        if not rt:
            continue
        dt = _parse_ts(str(rt))
        if dt is None:
            continue
        delta = abs((dt - ts).total_seconds())
        if best is None or delta < best_s:
            best, best_s = row, delta
    return best, best_s


def _fetch_klines(symbol: str, start_ms: int, end_ms: int) -> list[list[Any]]:
    out: list[list[Any]] = []
    cursor = start_ms
    while cursor < end_ms:
        params = urllib.parse.urlencode(
            {
                "symbol": symbol.upper(),
                "interval": "5m",
                "startTime": cursor,
                "endTime": end_ms,
                "limit": 500,
            }
        )
        with urllib.request.urlopen(f"{FAPI}?{params}", timeout=20) as resp:
            batch = json.load(resp)
        if not batch:
            break
        out.extend(batch)
        if len(batch) < 500:
            break
        cursor = int(batch[-1][0]) + 1
    return out


def _forward_stats(
    *,
    direction: str,
    entry: float,
    kl: list[list[Any]],
    slippage_pct: float = 0.0,
    win_min: float = 2.0,
    loss_max: float = -2.0,
) -> dict[str, Any]:
    if not kl or entry <= 0:
        return {"error": "no_klines"}
    slip = max(0.0, slippage_pct) / 100.0
    entry_eff = entry * (1.0 + slip)
    hi = max(float(k[2]) for k in kl)
    lo = min(float(k[3]) for k in kl)
    last = float(kl[-1][4])
    if direction == "short":
        mfe = (entry_eff - lo) / entry_eff * 100.0
        mae = (hi - entry_eff) / entry_eff * 100.0
        pnl = (entry_eff - last) / entry_eff * 100.0
    else:
        mfe = (hi - entry_eff) / entry_eff * 100.0
        mae = (entry_eff - lo) / entry_eff * 100.0
        pnl = (last - entry_eff) / entry_eff * 100.0
    label = "win" if pnl >= win_min else "loss" if pnl <= loss_max else "flat"
    return {
        "entry_eff": round(entry_eff, 8),
        "slippage_pct": round(slippage_pct, 3),
        "mfe_pct": round(mfe, 2),
        "mae_pct": round(mae, 2),
        "pnl_pct": round(pnl, 2),
        "label": label,
        "bars": len(kl),
    }


def _collect_sends() -> list[dict[str, Any]]:
    tg = json.loads(TELEGRAM_COOLDOWN.read_text(encoding="utf-8"))
    sends: list[dict[str, Any]] = []
    for key, ts_raw in tg.items():
        ts = _parse_ts(ts_raw)
        if ts is None:
            continue
        if key.startswith("early:"):
            _, sym, direction, tier = key.split(":", 3)
            sends.append(
                {
                    "channel": "early",
                    "tier": tier,
                    "symbol": sym.upper(),
                    "direction": direction.lower(),
                    "ts": ts,
                    "key": key,
                }
            )
        elif key.endswith(":squeeze"):
            sends.append(
                {
                    "channel": "squeeze",
                    "symbol": key.replace(":squeeze", "").upper(),
                    "direction": "",
                    "ts": ts,
                    "key": key,
                }
            )
        elif ":" in key:
            sym, direction = key.rsplit(":", 1)
            sends.append(
                {
                    "channel": "confirmed",
                    "symbol": sym.upper(),
                    "direction": direction.lower(),
                    "ts": ts,
                    "key": key,
                }
            )
    sends.sort(key=lambda s: s["ts"])
    return sends


def _tracker_row(symbol: str, direction: str) -> dict[str, Any] | None:
    state = json.loads(SIGNAL_STATE.read_text(encoding="utf-8"))
    return (state.get("signals") or {}).get(f"{symbol.upper()}:{direction.lower()}")


def run_backtest(*, hours: float, slippage_pct: float | None = None) -> dict[str, Any]:
    st = stats_thresholds()
    slip = float(slippage_pct if slippage_pct is not None else st.get("meme_slippage_pct", 0.15))
    win_min = float(st.get("win_label_min_pct", 2.0))
    loss_max = float(st.get("loss_label_max_pct", -2.0))
    ticks = _load_ticks()
    sends = _collect_sends()
    rows: list[dict[str, Any]] = []
    for send in sends:
        if send["channel"] == "squeeze":
            continue
        sym = send["symbol"]
        direction = send.get("direction") or "short"
        tick, lag_s = _nearest_tick(ticks, sym, send["ts"])
        entry = float(tick.get("price") or 0) if tick else 0.0
        setup = (
            (tick.get("dump") if direction == "short" else tick.get("long")) or {}
            if tick
            else {}
        )
        levels = setup.get("levels") or {}
        start_ms = int(send["ts"].timestamp() * 1000)
        end_ms = start_ms + int(hours * 3600 * 1000)
        kl = _fetch_klines(sym, start_ms, end_ms) if entry > 0 else []
        fwd = _forward_stats(
            direction=direction,
            entry=entry,
            kl=kl,
            slippage_pct=slip,
            win_min=win_min,
            loss_max=loss_max,
        )
        tr = _tracker_row(sym, direction) if send["channel"] == "confirmed" else None
        rows.append(
            {
                **send,
                "ts": send["ts"].isoformat(),
                "entry": entry or None,
                "tick_lag_s": round(lag_s, 1) if lag_s is not None else None,
                "fuel": setup.get("dump_fuel") or setup.get("long_fuel"),
                "lifecycle_phase": (tick.get("lifecycle") or {}).get("phase") if tick else None,
                "row_error": tick.get("error") if tick else "no_tick",
                "confirmed_setup": setup.get("confirmed"),
                "levels": {
                    "sl": levels.get("stop_loss"),
                    "tp1": levels.get("tp1"),
                    "tp2": levels.get("tp2"),
                },
                "forward": fwd,
                "tracker": (
                    {
                        "status": tr.get("status"),
                        "close_reason": tr.get("close_reason"),
                        "pnl_pct": tr.get("pnl_pct"),
                        "tp1_hit": tr.get("tp1_hit"),
                    }
                    if tr
                    else None
                ),
            }
        )

    early = [r for r in rows if r["channel"] == "early"]
    conf = [r for r in rows if r["channel"] == "confirmed"]
    ec = Counter(r["forward"].get("label") for r in early if "label" in r.get("forward", {}))
    cc = Counter(r["forward"].get("label") for r in conf if "label" in r.get("forward", {}))
    empty = [r for r in rows if not r.get("entry")]

    return {
        "ts": datetime.now(UTC).isoformat(),
        "hours_forward": hours,
        "slippage_pct": slip,
        "win_label_min_pct": win_min,
        "loss_label_max_pct": loss_max,
        "totals": {
            "tg_keys": len(json.loads(TELEGRAM_COOLDOWN.read_text())),
            "backtested": len(rows),
            "early": len(early),
            "confirmed": len(conf),
            "empty_entry": len(empty),
        },
        "early_outcomes": dict(ec),
        "confirmed_outcomes": dict(cc),
        "worst": sorted(
            [r for r in rows if r.get("forward", {}).get("label") == "loss"],
            key=lambda x: x.get("forward", {}).get("pnl_pct", 0),
        )[:15],
        "best": sorted(
            [r for r in rows if r.get("forward", {}).get("label") == "win"],
            key=lambda x: -x.get("forward", {}).get("pnl_pct", 0),
        )[:10],
        "rows": rows,
    }


def main() -> int:
    st = stats_thresholds()
    default_hours = float(st.get("forward_horizon_hours", 8.0))
    default_slip = float(st.get("meme_slippage_pct", 0.15))
    parser = argparse.ArgumentParser(description="Backtest Hunt Telegram sends")
    parser.add_argument(
        "--hours",
        type=float,
        default=default_hours,
        help="Forward window per send (default from param_store.stats)",
    )
    parser.add_argument(
        "--slippage-pct",
        type=float,
        default=None,
        help=f"Adverse fill stress %% (default {default_slip})",
    )
    args = parser.parse_args()
    report = run_backtest(hours=args.hours, slippage_pct=args.slippage_pct)
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")

    t = report["totals"]
    print(f"TG backtest · early={t['early']} confirmed={t['confirmed']} empty={t['empty_entry']}")
    print(f"Early {report['hours_forward']}h:   {report['early_outcomes']}")
    print(f"Confirm {report['hours_forward']}h: {report['confirmed_outcomes']}")
    print(f"Slippage stress: {report['slippage_pct']}%")
    print(f"Report: {REPORT_PATH}")
    for r in report["worst"][:8]:
        fwd = r.get("forward") or {}
        print(
            f"  LOSS {r['channel']:9} {r.get('tier','-'):5} {r['symbol']:12} {r['direction']:5} "
            f"pnl={fwd.get('pnl_pct')}% lc={r.get('lifecycle_phase')}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
