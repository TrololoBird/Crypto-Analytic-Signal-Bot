#!/usr/bin/env python3
"""VELVETUSDT-only minute watch: max public data + dump-point scoring.

Appends JSON lines to data/velvet_minute_watch.jsonl (never truncates).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import signal
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

try:
    from scripts.common import bootstrap_repo_path, configure_script_logging
except ModuleNotFoundError:  # pragma: no cover
    from common import bootstrap_repo_path, configure_script_logging

bootstrap_repo_path()

from bot.delivery.confluence import ConfluenceEngine
from bot.domain.config import load_settings
from bot.domain.schemas import SymbolFrames, UniverseSymbol
from bot.engine import SignalEngine, StrategyRegistry
from bot.features.prepare import _prepare_frame, min_required_bars, prepare_symbol
from bot.market._ws_parsers import depth_imbalance_from_book, microprice_bias_from_book
from bot.market.data import BinanceFuturesMarketData
from bot.market.rest_impl import BinanceClientImpl
from bot.market.universe import strategy_fits_for_market_row
from bot.runtime.delivery_orchestrator import DeliveryOrchestrator
from bot.runtime.errors import DEFENSIVE_EXC
from bot.setups.base import SetupParams
from bot.strategies import STRATEGY_CLASSES
from bot.strategies._common import _pivot_rows, with_spec_columns

SYMBOL = "VELVETUSDT"
OUT_PATH = Path("data/velvet_minute_watch.jsonl")

LOG = configure_script_logging("scripts.velvet_minute_watch")
_STOP = False


def _on_signal(*_args: object) -> None:
    global _STOP
    _STOP = True


def _col(df: Any, name: str, default: float = 0.0) -> float:
    if df is None or df.is_empty() or name not in df.columns:
        return default
    try:
        return float(df.item(-1, name))
    except (TypeError, ValueError):
        return default


def _candle_shape(df: Any) -> dict[str, float]:
    o = _col(df, "open")
    h = _col(df, "high")
    l = _col(df, "low")
    c = _col(df, "close")
    body = abs(c - o)
    full = max(h - l, 1e-12)
    upper_wick = h - max(o, c)
    lower_wick = min(o, c) - l
    return {
        "open": round(o, 6),
        "high": round(h, 6),
        "low": round(l, 6),
        "close": round(c, 6),
        "upper_wick_ratio": round(upper_wick / full, 3),
        "lower_wick_ratio": round(lower_wick / full, 3),
        "body_ratio": round(body / full, 3),
        "bearish": c < o,
        "bullish": c > o,
    }


def _tf_snapshot(df: Any) -> dict[str, Any]:
    if df is None or df.is_empty():
        return {"status": "empty"}
    c = _col(df, "close")
    e20, e50 = _col(df, "ema20"), _col(df, "ema50")
    spec = with_spec_columns(df)
    highs = _pivot_rows(spec, price_column="high", indicator_column="rsi14", pivot="high")
    lows = _pivot_rows(spec, price_column="low", indicator_column="rsi14", pivot="low")
    bear_div = bull_div = False
    if len(highs) >= 2:
        o, n = highs[-2], highs[-1]
        bear_div = n["price"] > o["price"] and n["indicator"] < o["indicator"]
    if len(lows) >= 2:
        o, n = lows[-2], lows[-1]
        bull_div = n["price"] < o["price"] and n["indicator"] > o["indicator"]
    return {
        "close": round(c, 6),
        "rsi14": round(_col(df, "rsi14", 50), 2),
        "atr14": round(_col(df, "atr14"), 6),
        "atr_pct": round(_col(df, "atr14") / c * 100, 2) if c else None,
        "adx14": round(_col(df, "adx14"), 2),
        "ema20": round(e20, 6),
        "ema50": round(e50, 6),
        "dist_ema20_pct": round((c / e20 - 1) * 100, 2) if e20 else None,
        "macd_hist": round(_col(df, "macd_hist"), 6),
        "vol_ratio": round(_col(df, "volume_ratio20", 1), 2),
        "delta_ratio": round(_col(df, "delta_ratio", 0.5), 3) if "delta_ratio" in df.columns else None,
        "trend": "bull" if c > e20 > e50 else ("bear" if c < e20 < e50 else "mixed"),
        "bearish_rsi_div": bear_div,
        "bullish_rsi_div": bull_div,
        "candle": _candle_shape(df),
    }


def _fib_levels(high: float, low: float) -> dict[str, float]:
    leg = high - low
    return {
        "ext_1272": round(high + leg * 0.272, 6),
        "ext_1618": round(high + leg * 0.618, 6),
        "ret_236": round(high - leg * 0.236, 6),
        "ret_382": round(high - leg * 0.382, 6),
        "ret_50": round(high - leg * 0.5, 6),
    }


def _dump_analysis(
    *,
    price: float,
    tf: dict[str, Any],
    positioning: dict[str, Any],
    impulse_high: float,
    fib: dict[str, float],
    short_hits: list[dict[str, Any]],
    prev_oi: float | None,
    cur_oi: float | None,
) -> dict[str, Any]:
    triggers: list[str] = []
    score = 0.0

    r15 = tf.get("15m", {})
    r1 = tf.get("1m", {})
    r5 = tf.get("5m", {})
    r1h = tf.get("1h", {})
    r4h = tf.get("4h", {})

    if r15.get("rsi14", 0) >= 72:
        score += 12
        triggers.append("rsi15_overbought")
    if r1h.get("rsi14", 0) >= 72:
        score += 10
        triggers.append("rsi1h_overbought")
    if r4h.get("bearish_rsi_div"):
        score += 15
        triggers.append("bear_div_4h")
    if r1h.get("bearish_rsi_div"):
        score += 12
        triggers.append("bear_div_1h")

    c1 = r1.get("candle", {})
    if c1.get("bearish") and c1.get("upper_wick_ratio", 0) >= 0.45:
        score += 18
        triggers.append("1m_rejection_wick")
    c5 = r5.get("candle", {})
    if c5.get("bearish") and c5.get("upper_wick_ratio", 0) >= 0.4:
        score += 14
        triggers.append("5m_rejection_wick")

    if price >= fib.get("ext_1272", 0) * 0.985:
        score += 10
        triggers.append("at_fib_1272")
    if price > impulse_high * 1.05:
        score += 8
        triggers.append("extended_above_impulse_high")

    support_trigger = round(max(impulse_high, 0.452), 6)
    if price < support_trigger:
        score += 25
        triggers.append(f"lost_support_{support_trigger}")

    taker = positioning.get("taker_1h")
    if taker is not None and taker < 0.98:
        score += 10
        triggers.append("taker_sell_pressure")
    if prev_oi and cur_oi and cur_oi < prev_oi * 0.998:
        score += 8
        triggers.append("oi_flush")

    if short_hits:
        score += min(20, len(short_hits) * 7)
        triggers.append(f"bot_short_hits={len(short_hits)}")

    if score >= 70:
        phase = "dump_imminent"
    elif score >= 45:
        phase = "dump_setup_forming"
    elif score >= 25:
        phase = "exhaustion_watch"
    else:
        phase = "no_dump_yet"

    atr15 = float(r15.get("atr14") or 0.02)
    entry_short = round(max(price, fib.get("ext_1272", price) * 0.99), 6)
    stop = round(entry_short + atr15 * 0.9, 6)
    tp1 = round(fib.get("ret_236", price * 0.9), 6)

    return {
        "dump_score": round(score, 1),
        "phase": phase,
        "triggers": triggers,
        "support_break_level": support_trigger,
        "resistance_liq": fib.get("ext_1272"),
        "dump_entry_hint": [round(entry_short * 0.998, 6), round(entry_short * 1.008, 6)],
        "dump_stop_hint": stop,
        "dump_tp1_hint": tp1,
        "invalidation_above": round(fib.get("ext_1272", price) * 1.015, 6),
    }


async def _snapshot(*, prev_oi: float | None) -> dict[str, Any]:
    settings = load_settings()
    minimums = min_required_bars(
        min_bars_15m=settings.filters.min_bars_15m,
        min_bars_1h=settings.filters.min_bars_1h,
        min_bars_4h=settings.filters.min_bars_4h,
    )
    client = BinanceFuturesMarketData(
        binance_client=BinanceClientImpl(
            rest_timeout_seconds=45.0,
            futures_data_request_limit_per_5m=settings.runtime.futures_data_request_limit_per_5m,
            proxy_url=settings.network.proxy_url,
            trust_env=settings.network.trust_env,
        ),
    )
    try:
        exchange = await client.fetch_exchange_symbols()
        meta = next(r for r in exchange if r.symbol == SYMBOL)
        ticker = next(t for t in await client.fetch_ticker_24h() if t.get("symbol") == SYMBOL)
        price = float(ticker.get("last_price") or 0)
        market_row = {
            "symbol": SYMBOL,
            "base_asset": meta.base_asset,
            "quote_asset": meta.quote_asset,
            "contract_type": meta.contract_type,
            "status": meta.status,
            "onboard_date_ms": meta.onboard_date_ms,
            "quote_volume": float(ticker.get("quote_volume") or 0),
            "price_change_percent": float(ticker.get("price_change_percent") or 0),
            "price_change_pct": float(ticker.get("price_change_percent") or 0),
            "last_price": price,
            "trade_count": float(ticker.get("trade_count") or 0),
        }
        item = UniverseSymbol(
            symbol=SYMBOL,
            base_asset=meta.base_asset,
            quote_asset=meta.quote_asset,
            contract_type=meta.contract_type,
            status=meta.status,
            onboard_date_ms=meta.onboard_date_ms,
            quote_volume=market_row["quote_volume"],
            price_change_pct=market_row["price_change_percent"],
            last_price=price,
            shortlist_bucket="pinned",
            seed_source="velvet_minute_watch",
            strategy_fits=strategy_fits_for_market_row(market_row, settings=settings),
        )

        df_1m = await client.fetch_klines_cached(SYMBOL, "1m", limit=500)
        df_5m = await client.fetch_klines_cached(SYMBOL, "5m", limit=500)
        frames = SymbolFrames(
            symbol=SYMBOL,
            df_15m=await client.fetch_klines_cached(SYMBOL, "15m", limit=600),
            df_1h=await client.fetch_klines_cached(SYMBOL, "1h", limit=600),
            df_5m=df_5m,
            df_4h=await client.fetch_klines_cached(SYMBOL, "4h", limit=400),
            bid_price=None,
            ask_price=None,
        )
        book = await client._fetch_book_ticker_rest_detail(SYMBOL)
        frames.bid_price = book.get("bid_price")
        frames.ask_price = book.get("ask_price")
        frames.bid_qty = book.get("bid_qty")
        frames.ask_qty = book.get("ask_qty")

        for fn in (
            lambda: client.fetch_open_interest(SYMBOL),
            lambda: client.fetch_open_interest_change(SYMBOL, period="5m"),
            lambda: client.fetch_open_interest_change(SYMBOL, period="1h"),
            lambda: client.fetch_long_short_ratio(SYMBOL, period="5m"),
            lambda: client.fetch_long_short_ratio(SYMBOL, period="1h"),
            lambda: client.fetch_taker_ratio(SYMBOL, period="5m"),
            lambda: client.fetch_taker_ratio(SYMBOL, period="15m"),
            lambda: client.fetch_taker_ratio(SYMBOL, period="1h"),
            lambda: client.fetch_funding_rate(SYMBOL),
        ):
            try:
                await fn()
            except DEFENSIVE_EXC:
                continue

        prepared = prepare_symbol(item, frames, minimums=minimums, settings=settings)
        if prepared is None:
            return {"ts": datetime.now(UTC).isoformat(), "error": "prepare_failed"}

        work_1m = _prepare_frame(df_1m)
        delta = _col(prepared.work_15m, "delta_ratio", None)
        prepared.depth_imbalance = depth_imbalance_from_book(
            bid_qty=frames.bid_qty, ask_qty=frames.ask_qty, delta_ratio=delta,
        )
        prepared.microprice_bias = microprice_bias_from_book(
            bid=frames.bid_price,
            ask=frames.ask_price,
            bid_qty=frames.bid_qty,
            ask_qty=frames.ask_qty,
            delta_ratio=delta,
        )
        prepared.oi_current = client.get_cached_open_interest(SYMBOL)
        prepared.oi_change_pct = client.get_cached_oi_change(SYMBOL)
        prepared.ls_ratio = client.get_cached_ls_ratio(SYMBOL)
        prepared.taker_ratio = client.get_cached_taker_ratio(SYMBOL)
        prepared.funding_rate = client.get_cached_funding_rate(SYMBOL)
        premium = (await client.fetch_premium_index_all()).get(SYMBOL, {})

        lows4 = [float(x) for x in prepared.work_4h["low"].to_list()]
        highs4 = [float(x) for x in prepared.work_4h["high"].to_list()]
        window = 30
        seg = lows4[-window:]
        il = min(seg)
        idx = len(lows4) - window + seg.index(il)
        ih = max(highs4[idx:])
        fib = _fib_levels(ih, il)

        tf = {
            "1m": _tf_snapshot(work_1m),
            "5m": _tf_snapshot(prepared.work_5m),
            "15m": _tf_snapshot(prepared.work_15m),
            "1h": _tf_snapshot(prepared.work_1h),
            "4h": _tf_snapshot(prepared.work_4h),
        }

        registry = StrategyRegistry()
        for cls in STRATEGY_CLASSES:
            registry.register(cls(SetupParams(enabled=True), settings))
        engine = SignalEngine(registry, settings)
        conf_eng = ConfluenceEngine(settings)
        short_hits: list[dict[str, Any]] = []
        for r in await engine.calculate_all(prepared, event_interval="15m"):
            if r.signal is None or r.signal.direction != "short":
                continue
            conf = conf_eng.score(r.signal, prepared)
            gate, _, gd = DeliveryOrchestrator._hard_confluence_gate(
                r.signal, prepared, settings=settings, confluence_engine=conf_eng,
            )
            short_hits.append({
                "setup": r.signal.setup_id,
                "score": round(float(r.signal.score), 3),
                "conf": round(float(conf.final_score), 3),
                "gate": gate,
                "reason": gd.get("reason"),
                "stop": r.signal.stop,
                "tp1": r.signal.take_profit_1,
            })

        positioning = {
            "oi": prepared.oi_current,
            "oi_chg_5m": client.get_cached_oi_change(SYMBOL),
            "funding_pct": round((prepared.funding_rate or 0) * 100, 4),
            "ls_5m": client.get_cached_ls_ratio(SYMBOL),
            "ls_1h": prepared.ls_ratio,
            "taker_5m": client.get_cached_taker_ratio(SYMBOL),
            "taker_15m": client.get_cached_taker_ratio(SYMBOL),
            "taker_1h": prepared.taker_ratio,
            "depth_imbalance": prepared.depth_imbalance,
            "microprice_bias": prepared.microprice_bias,
            "mark": premium.get("mark_price"),
            "basis_pct": premium.get("basis_pct"),
            "bid": frames.bid_price,
            "ask": frames.ask_price,
        }

        dump = _dump_analysis(
            price=price,
            tf=tf,
            positioning=positioning,
            impulse_high=ih,
            fib=fib,
            short_hits=short_hits,
            prev_oi=prev_oi,
            cur_oi=prepared.oi_current,
        )

        return {
            "ts": datetime.now(UTC).isoformat(),
            "symbol": SYMBOL,
            "price": price,
            "chg_24h_pct": round(float(ticker.get("price_change_percent") or 0), 2),
            "vol_24h_m": round(float(ticker.get("quote_volume") or 0) / 1e6, 1),
            "positioning": positioning,
            "timeframes": tf,
            "impulse": {"low": round(il, 6), "high": round(ih, 6), "fib": fib},
            "bot_short_hits": short_hits,
            "dump": dump,
        }
    finally:
        await client.close()


async def _run_loop(interval_s: int, once: bool) -> None:
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    prev_oi: float | None = None
    while not _STOP:
        started = time.monotonic()
        try:
            row = await _snapshot(prev_oi=prev_oi)
            if row.get("positioning", {}).get("oi") is not None:
                prev_oi = float(row["positioning"]["oi"])
            with OUT_PATH.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, default=str) + "\n")
            dump = row.get("dump", {})
            LOG.info(
                "velvet_minute_tick",
                price=row.get("price"),
                dump_score=dump.get("dump_score"),
                phase=dump.get("phase"),
                triggers=dump.get("triggers"),
            )
            if once:
                print(json.dumps(row, indent=2, default=str))
                return
        except DEFENSIVE_EXC as exc:
            LOG.warning("velvet_minute_tick_failed", error=repr(exc))
        except Exception:
            LOG.exception("velvet_minute_tick_error")
        if once:
            return
        elapsed = time.monotonic() - started
        await asyncio.sleep(max(1.0, interval_s - elapsed))


def main() -> None:
    parser = argparse.ArgumentParser(description="VELVETUSDT 1-minute watch")
    parser.add_argument("--interval", type=int, default=60, help="Seconds between ticks")
    parser.add_argument("--once", action="store_true", help="Single snapshot to stdout + append")
    args = parser.parse_args()
    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)
    asyncio.run(_run_loop(args.interval, args.once))


if __name__ == "__main__":
    main()
