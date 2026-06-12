#!/usr/bin/env python3
"""Deep single-symbol monitor — max Binance REST + hunt probe + dump-init scoring.

Usage:
    PYTHONPATH=hunt python hunt/scripts/deep_symbol_watch.py ESPORTSUSDT --hours 12 --interval 45
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from hunt_watch.bootstrap import bootstrap

bootstrap()

import importlib.util

from engine.domain.config import load_settings
from engine.features.prepare_frame import _prepare_frame
from hunt_core.market import HuntCcxtClient
from engine.telegram import TelegramBroadcaster
from hunt_watch.dump_hunt_alert import (
    dump_hunt_skip_reason,
    format_dump_hunt_telegram,
    maybe_send_dump_hunt_telegram,
    tier_from_verdict,
)
from hunt_watch.dump_init_score import score_dump_init
from hunt_watch.paths import DATA
from hunt_watch.symbol_probe import probe_symbol_signal
from hunt_watch.watchlist_ops import add_to_watchlist, register_signal_notify

LOG = logging.getLogger("deep_symbol_watch")

KLIMITS = {"1m": 500, "3m": 480, "5m": 500, "15m": 500, "1h": 500, "4h": 300, "1d": 90}


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


async def _fetch_micro(client: HuntCcxtClient, symbol: str) -> dict[str, Any]:
    specs = [
        ("oi", client.fetch_open_interest(symbol)),
        ("oi_5m", client.fetch_open_interest_change(symbol, period="5m")),
        ("oi_1h", client.fetch_open_interest_change(symbol, period="1h")),
        ("oi_series", client.fetch_open_interest_series(symbol, period="5m", limit=48)),
        ("gls_series", client.fetch_global_ls_series(symbol, period="5m", limit=48)),
        ("funding", client.fetch_funding_rate(symbol)),
        ("funding_hist", client.fetch_funding_rate_history(symbol, limit=16)),
        ("taker_5m", client.fetch_taker_ratio(symbol, period="5m")),
        ("taker_15m", client.fetch_taker_ratio(symbol, period="15m")),
        ("taker_1h", client.fetch_taker_ratio(symbol, period="1h")),
        ("ls_5m", client.fetch_long_short_ratio(symbol, period="5m")),
        ("ls_1h", client.fetch_long_short_ratio(symbol, period="1h")),
        ("top_ls_5m", client.fetch_top_position_ls_ratio(symbol, period="5m")),
        ("top_ls_1h", client.fetch_top_position_ls_ratio(symbol, period="1h")),
        ("global_ls_5m", client.fetch_global_ls_ratio(symbol, period="5m")),
        ("global_ls_1h", client.fetch_global_ls_ratio(symbol, period="1h")),
        ("basis_5m", client.fetch_basis(symbol, period="5m")),
        ("agg", client.fetch_agg_trade_snapshot(symbol, limit=100)),
        ("book", client.fetch_order_book_depth_snapshot(symbol, limit=100)),
        ("premium", client.fetch_premium_index_all()),
    ]
    results = await asyncio.gather(*(c for _, c in specs), return_exceptions=True)
    out: dict[str, Any] = {}
    for (name, _), res in zip(specs, results, strict=True):
        out[name] = None if isinstance(res, BaseException) else res
    prem = out.get("premium") or {}
    prem_row = prem.get(symbol) or prem.get(symbol.upper()) if isinstance(prem, dict) else None
    agg = out.get("agg")
    book = out.get("book") or {}
    return {
        "oi": out.get("oi"),
        "oi_chg_5m": out.get("oi_5m"),
        "oi_chg_1h": out.get("oi_1h"),
        "oi_series_tail": (out.get("oi_series") or [])[-6:] if isinstance(out.get("oi_series"), list) else None,
        "gls_series_tail": (out.get("gls_series") or [])[-6:] if isinstance(out.get("gls_series"), list) else None,
        "funding_pct": round(float(out.get("funding") or 0) * 100, 4),
        "funding_hist_tail": (out.get("funding_hist") or [])[-4:] if isinstance(out.get("funding_hist"), list) else None,
        "taker_5m": out.get("taker_5m"),
        "taker_15m": out.get("taker_15m"),
        "taker_1h": out.get("taker_1h"),
        "ls_5m": out.get("ls_5m"),
        "ls_1h": out.get("ls_1h"),
        "top_ls_5m": out.get("top_ls_5m"),
        "top_ls_1h": out.get("top_ls_1h"),
        "global_ls_5m": out.get("global_ls_5m"),
        "global_ls_1h": out.get("global_ls_1h"),
        "basis_5m": out.get("basis_5m"),
        "premium_zscore_5m": (prem_row or {}).get("zscore_5m") if isinstance(prem_row, dict) else None,
        "agg_delta_ratio": getattr(agg, "delta_ratio", None) if agg else None,
        "agg_buy_qty": getattr(agg, "buy_qty", None) if agg else None,
        "agg_sell_qty": getattr(agg, "sell_qty", None) if agg else None,
        "book_bid_levels": book.get("bid_levels"),
        "book_ask_levels": book.get("ask_levels"),
        "book_bid_price": book.get("bid_price"),
        "book_ask_price": book.get("ask_price"),
    }


async def _maybe_telegram_dump_alert(
    broadcaster: TelegramBroadcaster | None,
    *,
    symbol: str,
    record: dict[str, Any],
    row: dict[str, Any],
    now: datetime,
) -> None:
    if broadcaster is None:
        return
    dump = row.get("dump") or {}
    lc = record.get("lifecycle") or {}
    price = float(record.get("price") or 0)
    fuel = float(dump.get("dump_fuel") or dump.get("dump_score") or 0)
    confirmed = bool(dump.get("confirmed"))
    verdict = str(record.get("dump_verdict") or "")
    tier = tier_from_verdict(verdict, confirmed=confirmed)
    if tier == "prep":
        return
    if tier is None and fuel >= 75 and not confirmed:
        tier = "prep"
        return
    if tier is None:
        return

    lc_dict = lc if isinstance(lc, dict) else {}
    imp_low = float(row.get("impulse_low") or (row.get("impulse") or {}).get("hunt_low") or 0)
    atr15 = float(((record.get("timeframes") or {}).get("15m") or {}).get("atr14") or 0)
    skip = dump_hunt_skip_reason(
        symbol=symbol,
        tier=tier,
        price=price,
        setup=dump,
        lifecycle=lc_dict,
        now=now,
    )
    if skip:
        LOG.info(
            "deep_watch_telegram_skipped symbol=%s tier=%s reason=%s price=%s",
            symbol,
            tier,
            skip,
            price,
        )
        return

    msg = format_dump_hunt_telegram(
        symbol=symbol,
        tier=tier,
        price=price,
        setup=dump,
        lifecycle=lc_dict,
        chg_24h=float(record.get("chg_24h_pct") or 0),
        dump_init_score=int(record.get("dump_init_score") or 0),
        dump_reasons=list(record.get("dump_reasons") or []),
        impulse_low=imp_low,
        atr15=atr15,
        note=f"deep_watch · {verdict}",
    )
    sent = await maybe_send_dump_hunt_telegram(
        broadcaster,
        symbol=symbol,
        tier=tier,
        message=msg,
        now=now,
        price=price,
        setup=dump,
        lifecycle=lc_dict,
    )
    if sent:
        LOG.warning(
            "deep_watch_telegram_sent symbol=%s tier=%s verdict=%s score=%s fuel=%s",
            symbol,
            tier,
            verdict,
            record.get("dump_init_score"),
            fuel,
        )


async def _one_pass(
    *,
    symbol: str,
    client: HuntCcxtClient,
    hw: Any,
    prev: dict[str, Any] | None,
    out_path: Path,
    broadcaster: TelegramBroadcaster | None = None,
) -> dict[str, Any]:
    klines = {
        tf: await client.fetch_klines_cached(symbol, tf, limit=lim)
        for tf, lim in KLIMITS.items()
    }
    micro = await _fetch_micro(client, symbol)
    row = await probe_symbol_signal(symbol)
    tf = {name: _tf_snap(hw, df) for name, df in klines.items()}

    ticker_rows = await client.fetch_ticker_24h()
    ticker = next((r for r in ticker_rows if r.get("symbol") == symbol), {})
    hi24 = float(ticker.get("high_price") or 0)
    lo24 = float(ticker.get("low_price") or 0)
    price = float(row.get("price") or ticker.get("last_price") or 0)
    pos24 = round((price - lo24) / (hi24 - lo24), 3) if hi24 > lo24 else 0.5

    dump = row.get("dump") or {}
    lc = row.get("lifecycle") or {}
    score, reasons, verdict = score_dump_init(row=row, micro=micro, tf=tf, prev=prev)

    record: dict[str, Any] = {
        "ts": datetime.now(UTC).isoformat(),
        "symbol": symbol,
        "price": price,
        "chg_24h_pct": round(float(ticker.get("price_change_percent") or 0), 2),
        "pos_in_range_24h": pos24,
        "dump_init_score": score,
        "dump_verdict": verdict,
        "dump_reasons": reasons,
        "lifecycle": {
            "phase": lc.get("phase"),
            "bias": lc.get("recommended_bias"),
            "fall_from_high_pct": lc.get("fall_from_high_pct"),
            "bounce_from_low_pct": lc.get("bounce_from_low_pct"),
        },
        "hunt_short": {
            "score": dump.get("dump_score"),
            "fuel": dump.get("dump_fuel"),
            "phase": dump.get("phase"),
            "confirmed": dump.get("confirmed"),
            "confirm_hard": dump.get("confirm_hard"),
            "support_break": dump.get("support_break_level"),
            "tp1": dump.get("tp1"),
            "tp2": dump.get("tp2"),
            "stop_loss": dump.get("stop_loss"),
        },
        "impulse_high": row.get("impulse_high"),
        "impulse_low": row.get("impulse_low"),
        "microstructure": micro,
        "market": row.get("market"),
        "regime": row.get("regime"),
        "timeframes": tf,
    }

    with out_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, default=str) + "\n")

    LOG.info(
        "deep_watch %s price=%s verdict=%s score=%s fall=%s short_conf=%s phase=%s",
        symbol,
        price,
        verdict,
        score,
        lc.get("fall_from_high_pct"),
        dump.get("confirmed"),
        lc.get("phase"),
    )
    if verdict in ("DUMP_ARMED", "DUMP_LIKELY") or dump.get("confirmed"):
        LOG.warning(
            "DUMP_ALERT %s verdict=%s score=%s reasons=%s tp1=%s sl=%s",
            symbol,
            verdict,
            score,
            reasons,
            dump.get("tp1"),
            dump.get("stop_loss"),
        )
    await _maybe_telegram_dump_alert(
        broadcaster,
        symbol=symbol,
        record=record,
        row=row,
        now=datetime.now(UTC),
    )
    return record


async def _run(args: argparse.Namespace) -> int:
    symbol = args.symbol.upper()
    if not symbol.endswith("USDT"):
        symbol = f"{symbol}USDT"

    out_path = DATA / f"deep_watch_{symbol}.jsonl"
    log_path = Path(__file__).resolve().parents[2] / "logs" / f"deep_watch_{symbol}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(log_path, encoding="utf-8"),
        ],
        force=True,
    )

    add_to_watchlist(
        symbol,
        source="deep_symbol_watch",
        hunt_score=99.0,
        watch_bias="short",
        note="dump hunt — max data collection",
        early_telegram=True,
        dump_hunt=True,
        notify_on_forming=True,
    )
    register_signal_notify(
        symbol,
        direction="short",
        phase="dump_setup_forming",
        notify_on_forming=True,
        min_fuel=65.0,
    )

    settings = load_settings()
    broadcaster: TelegramBroadcaster | None = None
    if settings.tg_token and settings.target_chat_id:
        broadcaster = TelegramBroadcaster(settings.tg_token, settings.target_chat_id)
        LOG.info("deep_watch_telegram_ready chat=%s", settings.target_chat_id)
    else:
        LOG.warning("deep_watch_telegram_disabled missing token/chat")
    client = HuntCcxtClient.from_settings(settings)
    hw = _load_watch()
    end_at = time.time() + args.hours * 3600.0
    prev: dict[str, Any] | None = None
    n = 0

    LOG.info("deep_watch_start symbol=%s hours=%s interval=%ss out=%s", symbol, args.hours, args.interval, out_path)

    try:
        while time.time() < end_at:
            n += 1
            try:
                prev = await _one_pass(
                    symbol=symbol,
                    client=client,
                    hw=hw,
                    prev=prev,
                    out_path=out_path,
                    broadcaster=broadcaster,
                )
            except Exception as exc:
                LOG.exception("deep_watch_pass_failed pass=%s err=%s", n, exc)
            sleep_s = min(float(args.interval), max(1.0, end_at - time.time()))
            await asyncio.sleep(sleep_s)
    finally:
        await client.close()

    LOG.info("deep_watch_done passes=%s", n)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Deep Binance + hunt dump-init monitor")
    parser.add_argument("symbol", nargs="?", default="ESPORTSUSDT")
    parser.add_argument("--hours", type=float, default=12.0)
    parser.add_argument("--interval", type=int, default=30, help="Seconds between full passes")
    args = parser.parse_args()
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
