#!/usr/bin/env python3
"""Multi-symbol pump/dump hunt — full REST analytics, Telegram on confirmed heuristic.

Lessons from VELVET dump: memecoin fade is valid even when main-bot htf_conflict blocks
delivery. Hunt symbols alert on closed-bar confirm + score; delivery_audit is advisory.
"""

from __future__ import annotations

import argparse
import asyncio
import html
import json
import signal
import time
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from hunt_watch.bootstrap import bootstrap

bootstrap()

from scripts.common import bootstrap_repo_path, configure_script_logging

bootstrap_repo_path()

from hunt_watch.levels import structural_long_levels, structural_short_levels
from hunt_watch.lifecycle import (
    apply_short_invalidation,
    assess_hunt_lifecycle,
    blocks_premature_exhaustion_short,
    effective_support_break,
)
from hunt_watch.paths import TELEGRAM_COOLDOWN, TICK_JSONL
from hunt_watch.signal_tracker import (
    evaluate_followups,
    load_tracker_state,
    mark_followups_sent,
    register_signal_open,
    save_tracker_state,
)
from hunt_watch.targets import effective_watch_mode, resolve_watch_universe

from bot.delivery.confluence import ConfluenceEngine
from bot.delivery.contract import validate_signal_contract
from bot.delivery.telegram import TelegramBroadcaster
from bot.domain.config import load_settings
from bot.domain.schemas import SymbolFrames, UniverseSymbol
from bot.engine import SignalEngine, StrategyRegistry
from bot.features.prepare import _prepare_frame, min_required_bars, prepare_symbol
from bot.market._ws_parsers import depth_imbalance_from_book, microprice_bias_from_book
from bot.market.data import BinanceFuturesMarketData
from bot.market.rest_impl import BinanceClientImpl
from bot.market.universe import strategy_fits_for_market_row
from bot.runtime.data_readiness import kline_fetch_limit
from bot.runtime.delivery_orchestrator import DeliveryOrchestrator
from bot.runtime.errors import DEFENSIVE_EXC
from bot.setups.base import SetupParams
from bot.strategies import STRATEGY_CLASSES
from bot.strategies._common import _pivot_rows, with_spec_columns

WatchMode = Literal["short", "long", "both"]

DEFAULT_SYMBOLS = ("JCTUSDT", "BEATUSDT", "VELVETUSDT", "HYPEUSDT", "BTCUSDT")
SYMBOL_WATCH_MODES: dict[str, WatchMode] = {
    "JCTUSDT": "short",
    "BEATUSDT": "both",  # lifecycle flips short→long after dump bounce (VELVET lesson)
    "VELVETUSDT": "long",  # post-dump: pump/bounce hunt
    "HYPEUSDT": "long",
    "BTCUSDT": "both",
}
# Shorter 4h window for volatile alts — 30 bars anchors ancient listing lows.
IMPULSE_WINDOW: dict[str, int] = {
    "VELVETUSDT": 12,
    "JCTUSDT": 12,
    "BEATUSDT": 12,
    "HYPEUSDT": 18,
    "BTCUSDT": 30,
}
# Hunt triggers use 1h swing for fast alts (48h = full pump leg); BTC uses 4h.
IMPULSE_WINDOW_1H: dict[str, int] = {
    "VELVETUSDT": 48,
    "JCTUSDT": 48,
    "BEATUSDT": 48,
    "HYPEUSDT": 72,
    "BTCUSDT": 168,
}
FAST_HUNT_SYMBOLS = frozenset({"JCTUSDT", "BEATUSDT", "VELVETUSDT"})
HUNT_SYMBOLS = frozenset(DEFAULT_SYMBOLS)
SYMBOL_TICK_TIMEOUT_S = 180


def _kline_limits(minimums: dict[str, int]) -> dict[str, int]:
    """Hunt watch pulls deeper history than default bot warmup (max 1500 bars)."""
    return {
        "1m": min(1500, max(1000, kline_fetch_limit(int(minimums.get("5m", 300)), "5m") * 2)),
        "3m": 480,
        "5m": kline_fetch_limit(int(minimums.get("5m", 300)), "5m"),
        "15m": kline_fetch_limit(int(minimums.get("15m", 400)), "15m"),
        "1h": kline_fetch_limit(int(minimums.get("1h", 400)), "1h"),
        "4h": kline_fetch_limit(int(minimums.get("4h", 200)), "4h"),
        "1d": 90,
    }


def _swing_range(work: Any, *, window: int) -> tuple[float, float]:
    if work is None or work.is_empty():
        return 0.0, 0.0
    lows = [float(x) for x in work["low"].to_list()]
    highs = [float(x) for x in work["high"].to_list()]
    w = min(window, len(lows))
    if w < 2:
        return highs[-1], lows[-1]
    seg = lows[-w:]
    il = min(seg)
    idx = len(lows) - w + seg.index(il)
    ih = max(highs[idx:])
    return ih, il


def _impulse_context(work_4h: Any, work_1h: Any, symbol: str) -> dict[str, Any]:
    sym = symbol.upper()
    ih4, il4 = _swing_range(work_4h, window=IMPULSE_WINDOW.get(sym, 30))
    ih1, il1 = _swing_range(work_1h, window=IMPULSE_WINDOW_1H.get(sym, 48))
    use_1h = sym in FAST_HUNT_SYMBOLS
    return {
        "impulse_high_4h": round(ih4, 6),
        "impulse_low_4h": round(il4, 6),
        "impulse_high_1h": round(ih1, 6),
        "impulse_low_1h": round(il1, 6),
        "hunt_high": round(ih1 if use_1h else ih4, 6),
        "hunt_low": round(il1 if use_1h else il4, 6),
        "impulse_source": "1h" if use_1h else "4h",
    }


def _session_stats(work_1m: Any, *, bars: int = 1440) -> dict[str, Any]:
    if work_1m is None or work_1m.is_empty():
        return {}
    n = min(bars, work_1m.height)
    highs = [float(x) for x in work_1m["high"].to_list()[-n:]]
    lows = [float(x) for x in work_1m["low"].to_list()[-n:]]
    closes = [float(x) for x in work_1m["close"].to_list()[-n:]]
    hi, lo, last = max(highs), min(lows), closes[-1]
    mid = (hi + lo) / 2.0 if hi > lo else last
    return {
        "high_24h": round(hi, 6),
        "low_24h": round(lo, 6),
        "range_pct_24h": round((hi / lo - 1) * 100, 2) if lo else None,
        "pos_in_range": round((last - lo) / (hi - lo), 3) if hi > lo else 0.5,
        "bars_1m_used": n,
    }


OUT_PATH = TICK_JSONL
STATE_PATH = TELEGRAM_COOLDOWN
SCAN_INTERVAL_S = 900
COOLDOWN_MINUTES = 45
FORMING_MIN_SCORE = 45

LOG = configure_script_logging("scripts.dump_minute_watch")
_STOP = False


def _watch_mode(symbol: str, mode_map: dict[str, WatchMode] | None = None) -> WatchMode:
    sym = symbol.upper()
    if mode_map and sym in mode_map:
        return mode_map[sym]
    return SYMBOL_WATCH_MODES.get(sym, "short")


def _mode_label(mode: WatchMode) -> str:
    return {"short": "🔻 SHORT", "long": "🟢 LONG", "both": "↔ ANY"}[mode]


async def _safe_fetch(coro: Any) -> Any:
    try:
        return await coro
    except DEFENSIVE_EXC:
        return None


async def _fetch_rest_pack(client: BinanceFuturesMarketData, symbol: str) -> dict[str, Any]:
    """Fetch all public REST enrichment the main bot uses (WS substitutes via REST)."""
    specs: list[tuple[str, Any]] = [
        ("oi", client.fetch_open_interest(symbol)),
        ("oi_chg_5m", client.fetch_open_interest_change(symbol, period="5m")),
        ("oi_chg_1h", client.fetch_open_interest_change(symbol, period="1h")),
        ("ls_5m", client.fetch_long_short_ratio(symbol, period="5m")),
        ("ls_1h", client.fetch_long_short_ratio(symbol, period="1h")),
        ("top_ls_5m", client.fetch_top_position_ls_ratio(symbol, period="5m")),
        ("top_ls_1h", client.fetch_top_position_ls_ratio(symbol, period="1h")),
        ("global_ls_5m", client.fetch_global_ls_ratio(symbol, period="5m")),
        ("global_ls_1h", client.fetch_global_ls_ratio(symbol, period="1h")),
        ("taker_5m", client.fetch_taker_ratio(symbol, period="5m")),
        ("taker_15m", client.fetch_taker_ratio(symbol, period="15m")),
        ("taker_1h", client.fetch_taker_ratio(symbol, period="1h")),
        ("funding", client.fetch_funding_rate(symbol)),
        ("basis_5m", client.fetch_basis(symbol, period="5m")),
        ("agg_trades", client.fetch_agg_trade_snapshot(symbol, limit=100)),
        ("book_depth", client.fetch_order_book_depth_snapshot(symbol, limit=20)),
    ]
    results = await asyncio.gather(*(c for _, c in specs), return_exceptions=True)
    pack: dict[str, Any] = {}
    for (name, _), res in zip(specs, results, strict=True):
        pack[name] = None if isinstance(res, BaseException) else res
    depth = pack.get("book_depth")
    if not isinstance(depth, dict) or not depth.get("bid_price"):
        pack["book_ticker"] = await _safe_fetch(client._fetch_book_ticker_rest_detail(symbol))
    return pack


def _book_from_pack(pack: dict[str, Any]) -> dict[str, float | None]:
    depth = pack.get("book_depth")
    if isinstance(depth, dict) and depth.get("bid_price"):
        return depth
    ticker = pack.get("book_ticker")
    return ticker if isinstance(ticker, dict) else {}


def _btc_corr_1h(sym_work_1h: Any, btc_work_1h: Any, *, lookback: int = 24) -> float | None:
    if (
        sym_work_1h is None
        or btc_work_1h is None
        or sym_work_1h.is_empty()
        or btc_work_1h.is_empty()
        or sym_work_1h.height < lookback + 2
        or btc_work_1h.height < lookback + 2
    ):
        return None
    sym_c = [float(x) for x in sym_work_1h["close"].to_list()[-(lookback + 1) :]]
    btc_c = [float(x) for x in btc_work_1h["close"].to_list()[-(lookback + 1) :]]
    sym_r = [(sym_c[i] / sym_c[i - 1] - 1.0) for i in range(1, len(sym_c))]
    btc_r = [(btc_c[i] / btc_c[i - 1] - 1.0) for i in range(1, len(btc_c))]
    n = min(len(sym_r), len(btc_r))
    if n < 8:
        return None
    sym_r, btc_r = sym_r[-n:], btc_r[-n:]
    mean_s = sum(sym_r) / n
    mean_b = sum(btc_r) / n
    cov = sum((sym_r[i] - mean_s) * (btc_r[i] - mean_b) for i in range(n))
    var_s = sum((x - mean_s) ** 2 for x in sym_r)
    var_b = sum((x - mean_b) ** 2 for x in btc_r)
    if var_s <= 0 or var_b <= 0:
        return None
    return round(cov / (var_s**0.5 * var_b**0.5), 4)


def _apply_rest_enrichments(
    prepared: Any,
    *,
    client: BinanceFuturesMarketData,
    symbol: str,
    pack: dict[str, Any],
    book: dict[str, float | None],
    premium_row: dict[str, float] | None,
    funding_info: dict[str, float | int] | None,
    delta: float | None,
) -> None:
    prepared.oi_current = pack.get("oi") or client.get_cached_open_interest(symbol)
    prepared.oi_change_pct = pack.get("oi_chg_1h") or client.get_cached_oi_change(symbol, "1h")
    prepared.ls_ratio = pack.get("ls_1h") or client.get_cached_ls_ratio(symbol, "1h")
    prepared.top_account_ls_ratio = prepared.ls_ratio
    prepared.top_position_ls_ratio = pack.get(
        "top_ls_1h"
    ) or client.get_cached_top_position_ls_ratio(symbol, "1h")
    prepared.top_trader_position_ratio = prepared.top_position_ls_ratio
    prepared.global_ls_ratio = pack.get("global_ls_1h") or client.get_cached_global_ls_ratio(
        symbol, "1h"
    )
    prepared.global_account_ls_ratio = prepared.global_ls_ratio
    if prepared.ls_ratio is not None and prepared.global_ls_ratio is not None:
        prepared.top_vs_global_ls_gap = float(prepared.ls_ratio) - float(prepared.global_ls_ratio)
    prepared.taker_ratio = pack.get("taker_1h") or client.get_cached_taker_ratio(symbol, "1h")
    prepared.funding_rate = pack.get("funding") or client.get_cached_funding_rate(symbol)
    prepared.funding_trend = client.get_cached_funding_trend(symbol)
    funding_z = client.get_cached_funding_rate_zscore(symbol)
    if funding_z is not None:
        prepared.funding_rate_zscore_48h = float(funding_z)
    extreme = client.get_cached_funding_recent_extreme(symbol)
    if extreme is not None:
        prepared.funding_recent_extreme_rate = float(extreme[0])
        prepared.funding_recent_extreme_age_hours = float(extreme[1])
    basis_stats = client.get_cached_basis_stats(symbol, period="5m")
    if basis_stats:
        if basis_stats.get("basis_pct") is not None:
            prepared.basis_pct = float(basis_stats["basis_pct"])
        if basis_stats.get("premium_zscore_5m") is not None:
            prepared.premium_zscore_5m = float(basis_stats["premium_zscore_5m"])
        if basis_stats.get("premium_slope_5m") is not None:
            prepared.premium_slope_5m = float(basis_stats["premium_slope_5m"])
    basis_direct = pack.get("basis_5m")
    if basis_direct is not None and prepared.basis_pct is None:
        prepared.basis_pct = float(basis_direct)
        prepared.mark_index_spread_bps = float(basis_direct) * 100.0
    if premium_row:
        mark = float(premium_row.get("mark_price") or 0.0)
        index = float(premium_row.get("index_price") or 0.0)
        if mark > 0:
            prepared.mark_price = mark
        if "funding_rate" in premium_row and prepared.funding_rate is None:
            prepared.funding_rate = float(premium_row.get("funding_rate") or 0.0)
        if mark > 0 and index > 0:
            basis = (mark / index - 1.0) * 100.0
            prepared.basis_pct = basis
            prepared.mark_index_spread_bps = basis * 100.0
        if premium_row.get("estimated_settle_price"):
            prepared.estimated_settle_price = float(premium_row["estimated_settle_price"])
        if premium_row.get("interest_rate") is not None:
            prepared.interest_rate = float(premium_row["interest_rate"])
        if premium_row.get("next_funding_time_ms"):
            prepared.next_funding_time_ms = int(premium_row["next_funding_time_ms"])
    if funding_info:
        if funding_info.get("funding_rate_cap") is not None:
            prepared.funding_rate_cap = float(funding_info["funding_rate_cap"])
        if funding_info.get("funding_rate_floor") is not None:
            prepared.funding_rate_floor = float(funding_info["funding_rate_floor"])
        if funding_info.get("funding_interval_hours") is not None:
            prepared.funding_interval_hours = int(funding_info["funding_interval_hours"])
    prepared.depth_imbalance = depth_imbalance_from_book(
        bid_qty=book.get("bid_qty"),
        ask_qty=book.get("ask_qty"),
        delta_ratio=delta,
    )
    prepared.microprice_bias = microprice_bias_from_book(
        bid=book.get("bid_price"),
        ask=book.get("ask_price"),
        bid_qty=book.get("bid_qty"),
        ask_qty=book.get("ask_qty"),
        delta_ratio=delta,
    )
    prepared.depth_imbalance_source = "rest_depth" if pack.get("book_depth") else "rest_ticker"
    prepared.microprice_bias_source = prepared.depth_imbalance_source
    agg = pack.get("agg_trades")
    if agg is not None:
        prepared.agg_trade_delta_30s = getattr(agg, "delta_ratio", None)
        prepared.orderflow_source = "agg_trade_rest"
    prepared.data_source_mix = "futures_rest_full"


def _market_snapshot(
    prepared: Any,
    *,
    pack: dict[str, Any],
    book: dict[str, float | None],
    premium_row: dict[str, float] | None,
    ticker: dict[str, Any],
) -> dict[str, Any]:
    agg = pack.get("agg_trades")
    bid_px = book.get("bid_price")
    ask_px = book.get("ask_price")
    bid_qty = book.get("bid_qty")
    ask_qty = book.get("ask_qty")
    bid_depth_usd = (
        round(float(bid_px) * float(bid_qty), 2)
        if bid_px is not None and bid_qty is not None
        else None
    )
    ask_depth_usd = (
        round(float(ask_px) * float(ask_qty), 2)
        if ask_px is not None and ask_qty is not None
        else None
    )
    return {
        "bid": bid_px,
        "ask": ask_px,
        "bid_qty": bid_qty,
        "ask_qty": ask_qty,
        "bid_depth_usd": bid_depth_usd,
        "ask_depth_usd": ask_depth_usd,
        "spread_bps": prepared.spread_bps,
        "mark": prepared.mark_price,
        "basis_pct": prepared.basis_pct,
        "basis_bps": round(float(prepared.basis_pct) * 100.0, 2)
        if prepared.basis_pct is not None
        else None,
        "mark_index_spread_bps": prepared.mark_index_spread_bps,
        "premium_zscore_5m": prepared.premium_zscore_5m,
        "premium_slope_5m": prepared.premium_slope_5m,
        "funding_rate": prepared.funding_rate,
        "funding_pct": round((prepared.funding_rate or 0) * 100, 4),
        "funding_trend": prepared.funding_trend,
        "funding_zscore_48h": prepared.funding_rate_zscore_48h,
        "funding_cap": prepared.funding_rate_cap,
        "funding_floor": prepared.funding_rate_floor,
        "funding_interval_h": prepared.funding_interval_hours,
        "next_funding_time_ms": prepared.next_funding_time_ms,
        "oi": prepared.oi_current,
        "oi_chg_5m": pack.get("oi_chg_5m"),
        "oi_chg_1h": pack.get("oi_chg_1h") or prepared.oi_change_pct,
        "ls_5m": pack.get("ls_5m"),
        "ls_1h": prepared.ls_ratio,
        "top_ls_5m": pack.get("top_ls_5m"),
        "top_ls_1h": prepared.top_position_ls_ratio,
        "global_ls_5m": pack.get("global_ls_5m"),
        "global_ls_1h": prepared.global_ls_ratio,
        "top_vs_global_ls_gap": prepared.top_vs_global_ls_gap,
        "taker_5m": pack.get("taker_5m"),
        "taker_15m": pack.get("taker_15m"),
        "taker_1h": prepared.taker_ratio,
        "depth_imbalance": prepared.depth_imbalance,
        "microprice_bias": prepared.microprice_bias,
        "agg_trade_delta": getattr(agg, "delta_ratio", None) if agg else None,
        "agg_buy_qty": getattr(agg, "buy_qty", None) if agg else None,
        "agg_sell_qty": getattr(agg, "sell_qty", None) if agg else None,
        "vol_24h_m": round(float(ticker.get("quote_volume") or 0) / 1e6, 1),
        "trade_count_24h": ticker.get("trade_count"),
    }


def _regime_snapshot(prepared: Any) -> dict[str, Any]:
    return {
        "market_regime": prepared.market_regime,
        "regime_4h": prepared.regime_4h_confirmed,
        "regime_1h": prepared.regime_1h_confirmed,
        "bias_4h": prepared.bias_4h,
        "bias_1h": prepared.bias_1h,
        "structure_1h": prepared.structure_1h,
        "poc_1h": prepared.poc_1h,
        "poc_15m": prepared.poc_15m,
        "vah_1h": prepared.vah_1h,
        "val_1h": prepared.val_1h,
        "vah_15m": prepared.vah_15m,
        "val_15m": prepared.val_15m,
        "btc_corr_1h": prepared.btc_corr_1h,
    }


def _data_quality_report(
    prepared: Any,
    *,
    frames: SymbolFrames,
    df_1m: Any,
    pack: dict[str, Any],
    book: dict[str, float | None],
    tf: dict[str, Any],
) -> dict[str, Any]:
    fields = {
        "oi": prepared.oi_current,
        "funding": prepared.funding_rate,
        "ls_1h": prepared.ls_ratio,
        "taker_1h": prepared.taker_ratio,
        "depth": prepared.depth_imbalance,
        "microprice": prepared.microprice_bias,
        "mark": prepared.mark_price,
        "basis": prepared.basis_pct,
        "agg_flow": prepared.agg_trade_delta_30s,
        "global_ls": prepared.global_ls_ratio,
        "top_ls": prepared.top_position_ls_ratio,
    }
    return {
        "bars_1m": int(df_1m.height) if df_1m is not None and not df_1m.is_empty() else 0,
        "bars_3m": 0 if tf.get("3m", {}).get("status") == "empty" else 1,
        "bars_5m": int(frames.df_5m.height if frames.df_5m is not None else 0),
        "bars_1d": 0 if tf.get("1d", {}).get("status") == "empty" else 1,
        "bars_15m": int(frames.df_15m.height if frames.df_15m is not None else 0),
        "bars_1h": int(frames.df_1h.height if frames.df_1h is not None else 0),
        "bars_4h": int(frames.df_4h.height if frames.df_4h is not None else 0),
        "prepare_ok": True,
        "book_ok": book.get("bid_price") is not None and book.get("ask_price") is not None,
        "book_source": "depth"
        if pack.get("book_depth") and pack["book_depth"].get("bid_price")
        else "ticker",
        "closed_5m_ok": bool(tf.get("5m_closed", {}).get("closed_bar")),
        "closed_1m_ok": bool(tf.get("1m_closed", {}).get("closed_bar")),
        "fields_ok": {k: v is not None for k, v in fields.items()},
        "fields_missing": [k for k, v in fields.items() if v is None],
    }


def _on_signal(*_args: object) -> None:
    global _STOP
    _STOP = True


def _col(df: Any, name: str, default: float = 0.0, *, idx: int = -1) -> float:
    if df is None or df.is_empty() or name not in df.columns:
        return default
    try:
        return float(df.item(idx, name))
    except TypeError, ValueError, IndexError:
        return default


def _candle_shape(df: Any, *, idx: int = -1) -> dict[str, Any]:
    o, h, l, c = (
        _col(df, "open", idx=idx),
        _col(df, "high", idx=idx),
        _col(df, "low", idx=idx),
        _col(df, "close", idx=idx),
    )
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


def _tf_snapshot_lite(df: Any) -> dict[str, Any]:
    """OHLC-only snapshot when indicator warmup is insufficient (e.g. new listing 1d)."""
    if df is None or df.is_empty():
        return {"status": "empty"}
    c = _col(df, "close")
    if c <= 0.0:
        return {"status": "empty"}
    return {
        "close": round(c, 6),
        "rsi14": None,
        "atr14": None,
        "atr_pct": None,
        "adx14": None,
        "status": "lite",
        "bars": int(df.height),
    }


def _tf_snapshot(df: Any, *, closed: bool = False) -> dict[str, Any]:
    if df is None or df.is_empty():
        return {"status": "empty"}
    idx = -2 if closed and df.height >= 2 else -1
    c = _col(df, "close", idx=idx)
    e20, e50 = _col(df, "ema20", idx=idx), _col(df, "ema50", idx=idx)
    spec = with_spec_columns(df)
    highs = _pivot_rows(spec, price_column="high", indicator_column="rsi14", pivot="high")
    lows = _pivot_rows(spec, price_column="low", indicator_column="rsi14", pivot="low")
    bear_div = False
    bull_div = False
    if len(highs) >= 2:
        o, n = highs[-2], highs[-1]
        bear_div = n["price"] > o["price"] and n["indicator"] < o["indicator"]
    if len(lows) >= 2:
        o, n = lows[-2], lows[-1]
        bull_div = n["price"] < o["price"] and n["indicator"] > o["indicator"]
    return {
        "close": round(c, 6),
        "rsi14": round(_col(df, "rsi14", 50, idx=idx), 2),
        "atr14": round(_col(df, "atr14", idx=idx), 6),
        "atr_pct": round(_col(df, "atr14", idx=idx) / c * 100, 2) if c else None,
        "adx14": round(_col(df, "adx14", idx=idx), 2),
        "ema20": round(e20, 6),
        "ema50": round(e50, 6),
        "dist_ema20_pct": round((c / e20 - 1) * 100, 2) if e20 else None,
        "macd_hist": round(_col(df, "macd_hist", idx=idx), 6),
        "vol_ratio": round(_col(df, "volume_ratio20", 1, idx=idx), 2),
        "delta_ratio": round(_col(df, "delta_ratio", 0.5, idx=idx), 3)
        if "delta_ratio" in df.columns
        else None,
        "bb_pct_b": round(_col(df, "bb_pct_b", 0.5, idx=idx), 3)
        if "bb_pct_b" in df.columns
        else None,
        "stoch_k": round(_col(df, "stoch_k14", 50, idx=idx), 1)
        if "stoch_k14" in df.columns
        else None,
        "supertrend_dir": int(_col(df, "supertrend_dir", 0, idx=idx))
        if "supertrend_dir" in df.columns
        else None,
        "bearish_rsi_div": bear_div,
        "bullish_rsi_div": bull_div,
        "trend": "bull" if c > e20 > e50 else ("bear" if c < e20 < e50 else "mixed"),
        "candle": _candle_shape(df, idx=idx),
        "closed_bar": closed and df.height >= 2,
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
    market: dict[str, Any],
    regime: dict[str, Any],
    impulse_high: float,
    impulse_low: float,
    support_break_level: float,
    fib: dict[str, float],
    short_hits: list[dict[str, Any]],
    prev_oi: float | None,
    cur_oi: float | None,
    local_support: float,
    local_resistance: float,
) -> dict[str, Any]:
    triggers: list[str] = []
    score = 0.0
    r15, r1, r5, r1h, r4h = (
        tf.get("15m_closed") or tf.get("15m", {}),
        tf.get("1m_closed") or tf.get("1m", {}),
        tf.get("5m_closed") or tf.get("5m", {}),
        tf.get("1h", {}),
        tf.get("4h", {}),
    )

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

    c1, c5, c15 = r1.get("candle", {}), r5.get("candle", {}), r15.get("candle", {})
    if c1.get("bearish") and c1.get("upper_wick_ratio", 0) >= 0.4:
        score += 16
        triggers.append("1m_rejection")
    if c5.get("bearish") and c5.get("upper_wick_ratio", 0) >= 0.35:
        score += 14
        triggers.append("5m_rejection")
    if c15.get("upper_wick_ratio", 0) >= 0.5 and not c15.get("bullish", True):
        score += 10
        triggers.append("15m_rejection_wick")

    if price >= fib.get("ext_1272", 0) * 0.985:
        score += 10
        triggers.append("at_fib_1272")
    if price > impulse_high * 1.03:
        score += 8
        triggers.append("extended_above_impulse_high")

    support_trigger = round(support_break_level, 6)
    r5_live = tf.get("5m_closed", {}).get("close") or price
    if support_trigger and r5_live < support_trigger:
        score += 28
        triggers.append(f"lost_support_{support_trigger}")
    elif impulse_high and r5_live < round(impulse_high * 0.998, 6):
        score += 12
        triggers.append(f"below_impulse_high_{round(impulse_high * 0.998, 6)}")

    taker = market.get("taker_5m") or market.get("taker_1h")
    if taker is not None and taker < 0.98:
        score += 10
        triggers.append("taker_sell_pressure")
    if prev_oi and cur_oi and cur_oi < prev_oi * 0.997:
        score += 10
        triggers.append("oi_flush")
    micro = market.get("microprice_bias")
    if micro is not None and micro < -0.05:
        score += 8
        triggers.append("microprice_sell_bias")
    if regime.get("regime_4h") == "downtrend":
        score += 8
        triggers.append("regime_4h_bear")
    fund = market.get("funding_pct")
    if fund is not None and fund > 0.05:
        score += 6
        triggers.append("crowded_long_funding")
    if short_hits:
        score += min(18, len(short_hits) * 6)
        triggers.append(f"bot_short={len(short_hits)}")

    atr15 = float(r15.get("atr14") or price * 0.02)
    levels = structural_short_levels(
        price=price,
        impulse_high=impulse_high,
        impulse_low=impulse_low,
        fib=fib,
        atr15=atr15,
        local_support=local_support,
        local_resistance=local_resistance,
    )

    return {
        "dump_score": round(score, 1),
        "triggers": triggers,
        "support_break_level": support_trigger,
        "resistance_liq": fib.get("ext_1272"),
        "entry_zone": levels["entry_zone"],
        "stop_loss": levels["stop_loss"],
        "tp1": levels["tp1"],
        "tp2": levels["tp2"],
        "risk_reward": levels.get("risk_reward"),
        "invalidation_above": levels["invalidation_above"],
    }


def _confirm_dump(dump: dict[str, Any], tf: dict[str, Any]) -> tuple[bool, list[str]]:
    """Confirmed dump = closed-bar price action + score + HTF context."""
    hard: list[str] = []
    c5 = tf.get("5m_closed", {}).get("candle", {})
    c1 = tf.get("1m_closed", {}).get("candle", {})
    r5_close = tf.get("5m_closed", {}).get("close") or 0.0
    r15_rsi = (tf.get("15m_closed") or tf.get("15m", {})).get("rsi14", 0)
    support = dump.get("support_break_level") or 0.0
    r15_close = tf.get("15m_closed", {}).get("close") or 0.0

    if support and r5_close < support:
        hard.append("5m_close_below_support")
    if support and r15_close and r15_close < support:
        hard.append("15m_close_below_support")
    if c5.get("bearish") and c5.get("upper_wick_ratio", 0) >= 0.35 and r15_rsi >= 65:
        hard.append("5m_rejection_exhaustion")
    if c1.get("bearish") and c5.get("bearish") and c1.get("upper_wick_ratio", 0) >= 0.35:
        hard.append("1m_5m_bear_cascade")

    score = float(dump.get("dump_score") or 0)
    div = tf.get("1h", {}).get("bearish_rsi_div") or tf.get("4h", {}).get("bearish_rsi_div")
    confirmed = (
        bool(hard)
        and score >= 60
        and (div or score >= 68 or "oi_flush" in dump.get("triggers", []))
    )
    return confirmed, hard


def _phase(dump: dict[str, Any], confirmed: bool, *, lifecycle_note: str | None = None) -> str:
    if lifecycle_note:
        return lifecycle_note
    if confirmed:
        return "dump_confirmed"
    score = float(dump.get("dump_score") or 0)
    if score >= 70:
        return "dump_imminent"
    if score >= 45:
        return "dump_setup_forming"
    if score >= 25:
        return "exhaustion_watch"
    return "no_dump_yet"


def _long_analysis(
    *,
    price: float,
    tf: dict[str, Any],
    market: dict[str, Any],
    regime: dict[str, Any],
    impulse_low: float,
    impulse_high: float,
    fib: dict[str, float],
    long_hits: list[dict[str, Any]],
    prev_oi: float | None,
    cur_oi: float | None,
) -> dict[str, Any]:
    triggers: list[str] = []
    score = 0.0
    r15, r1, r5, r1h, r4h = (
        tf.get("15m_closed") or tf.get("15m", {}),
        tf.get("1m_closed") or tf.get("1m", {}),
        tf.get("5m_closed") or tf.get("5m", {}),
        tf.get("1h", {}),
        tf.get("4h", {}),
    )
    if r15.get("rsi14", 50) <= 32:
        score += 12
        triggers.append("rsi15_oversold")
    if r1h.get("rsi14", 50) <= 35:
        score += 10
        triggers.append("rsi1h_oversold")
    if r4h.get("bullish_rsi_div"):
        score += 15
        triggers.append("bull_div_4h")
    if r1h.get("bullish_rsi_div"):
        score += 12
        triggers.append("bull_div_1h")

    c1, c5, c15 = r1.get("candle", {}), r5.get("candle", {}), r15.get("candle", {})
    if c1.get("bullish") and c1.get("lower_wick_ratio", 0) >= 0.4:
        score += 16
        triggers.append("1m_bounce")
    if c5.get("bullish") and c5.get("lower_wick_ratio", 0) >= 0.35:
        score += 14
        triggers.append("5m_bounce")
    if c15.get("lower_wick_ratio", 0) >= 0.5 and c15.get("bullish"):
        score += 10
        triggers.append("15m_bounce_wick")

    support_zone = fib.get("ret_382") or impulse_low
    if support_zone and price <= support_zone * 1.015:
        score += 10
        triggers.append("at_fib_support")
    if price < impulse_low * 0.97:
        score += 8
        triggers.append("deep_below_impulse_low")

    resistance_break = round(impulse_high * 0.998, 6)
    r5_live = tf.get("5m_closed", {}).get("close") or price
    if r5_live > resistance_break:
        score += 28
        triggers.append(f"broke_resistance_{resistance_break}")

    taker = market.get("taker_5m") or market.get("taker_1h")
    if taker is not None and taker > 1.02:
        score += 10
        triggers.append("taker_buy_pressure")
    if prev_oi and cur_oi and cur_oi > prev_oi * 1.003:
        score += 10
        triggers.append("oi_build")
    micro = market.get("microprice_bias")
    if micro is not None and micro > 0.05:
        score += 8
        triggers.append("microprice_buy_bias")
    if regime.get("regime_4h") == "uptrend":
        score += 8
        triggers.append("regime_4h_bull")
    fund = market.get("funding_pct")
    if fund is not None and fund < -0.02:
        score += 6
        triggers.append("crowded_short_funding")
    drop_from_high = (impulse_high - price) / impulse_high if impulse_high else 0.0
    if drop_from_high >= 0.08 and r15.get("rsi14", 50) <= 38:
        score += 18
        triggers.append("post_dump_oversold_bounce")
    if drop_from_high >= 0.12 and c5.get("bullish") and c5.get("lower_wick_ratio", 0) >= 0.3:
        score += 14
        triggers.append("capitulation_wick")
    if long_hits:
        score += min(18, len(long_hits) * 6)
        triggers.append(f"bot_long={len(long_hits)}")

    atr15 = float(r15.get("atr14") or price * 0.02)
    levels = structural_long_levels(
        price=price,
        impulse_high=impulse_high,
        impulse_low=impulse_low,
        fib=fib,
        atr15=atr15,
        local_support=support_zone or impulse_low,
        local_resistance=resistance_break,
    )

    return {
        "long_score": round(score, 1),
        "triggers": triggers,
        "resistance_break_level": resistance_break,
        "support_zone": support_zone,
        "entry_zone": levels["entry_zone"],
        "stop_loss": levels["stop_loss"],
        "tp1": levels["tp1"],
        "tp2": levels["tp2"],
        "risk_reward": levels.get("risk_reward"),
        "invalidation_below": levels["invalidation_below"],
    }


def _confirm_long(long_setup: dict[str, Any], tf: dict[str, Any]) -> tuple[bool, list[str]]:
    hard: list[str] = []
    resistance = long_setup.get("resistance_break_level") or 0.0
    c1 = tf.get("1m_closed", {}).get("candle", {})
    c5 = tf.get("5m_closed", {}).get("candle", {})
    r5_close = tf.get("5m_closed", {}).get("close") or 0.0
    r15_rsi = tf.get("15m", {}).get("rsi14", 50)

    if resistance and r5_close > resistance:
        hard.append("5m_close_above_resistance")
    if c5.get("bullish") and c5.get("lower_wick_ratio", 0) >= 0.35 and r15_rsi <= 40:
        hard.append("5m_bounce_oversold")
    if c1.get("bullish") and c5.get("bullish") and c1.get("lower_wick_ratio", 0) >= 0.35:
        hard.append("1m_5m_bull_cascade")

    score = float(long_setup.get("long_score") or 0)
    div = tf.get("1h", {}).get("bullish_rsi_div") or tf.get("4h", {}).get("bullish_rsi_div")
    confirmed = (
        bool(hard)
        and score >= 60
        and (div or score >= 68 or "oi_build" in long_setup.get("triggers", []))
    )
    return confirmed, hard


def _setup_formed(setup: dict[str, Any], *, direction: str) -> bool:
    score_key = "dump_score" if direction == "short" else "long_score"
    return float(setup.get(score_key) or 0) >= FORMING_MIN_SCORE


def _should_alert(
    setup: dict[str, Any],
    *,
    direction: str,
    symbol: str,
    lifecycle: Any | None = None,
    row: dict[str, Any] | None = None,
) -> bool:
    if not bool(setup.get("confirmed")) or not _setup_formed(setup, direction=direction):
        return False
    if direction == "short" and lifecycle is not None:
        lc: dict[str, Any]
        if isinstance(lifecycle, dict):
            lc = lifecycle
            if lc.get("invalidate_short") or not lc.get("short_entry_ok", False):
                return False
        else:
            if lifecycle.invalidate_short or not lifecycle.short_entry_ok:
                return False
            lc = {
                "phase": lifecycle.phase.value,
                "fall_from_high_pct": lifecycle.fall_from_high_pct,
                "bounce_from_low_pct": lifecycle.bounce_from_low_pct,
            }
        session = (row or {}).get("session") or {}
        tf = (row or {}).get("tf") or {}
        has_bear_div = bool(
            (tf.get("1h") or {}).get("bearish_rsi_div")
            or (tf.get("4h") or {}).get("bearish_rsi_div")
        )
        blocked, _reason = blocks_premature_exhaustion_short(
            phase=str(lc.get("phase") or ""),
            fall_from_high_pct=float(lc.get("fall_from_high_pct") or 0),
            bounce_from_low_pct=float(lc.get("bounce_from_low_pct") or 0),
            pos_in_range=float(session.get("pos_in_range") or 0.5),
            has_bear_div=has_bear_div,
        )
        if blocked:
            return False
    # Hunt watch: heuristic confirm drives Telegram (memecoin fades fail main-bot HTF gate).
    if symbol.upper() in HUNT_SYMBOLS:
        return True
    delivery = setup.get("delivery") or {}
    return bool(delivery.get("would_deliver"))


def _evaluate_delivery(
    signal: Any,
    prepared: Any,
    *,
    settings: Any,
    conf_eng: ConfluenceEngine,
) -> dict[str, Any]:
    issues = validate_signal_contract(signal)
    contract_ok = not issues
    conf = conf_eng.score(signal, prepared)
    gate_ok, _legs, gate_details = DeliveryOrchestrator._hard_confluence_gate(
        signal,
        prepared,
        settings=settings,
        confluence_engine=conf_eng,
    )
    block_reason: str | None = None
    if not contract_ok:
        block_reason = issues[0].reason if issues else "contract_invalid"
    elif not gate_ok:
        block_reason = str(gate_details.get("reason") or "hard_confluence_gate")
    return {
        "deliverable": contract_ok and gate_ok,
        "contract_ok": contract_ok,
        "contract_issues": [i.reason for i in issues[:4]],
        "confluence": round(float(conf.final_score), 3),
        "gate_ok": gate_ok,
        "gate_reason": block_reason if not gate_ok else None,
    }


def _attach_delivery_meta(
    setup: dict[str, Any],
    hits: list[dict[str, Any]],
) -> None:
    deliverable = [h for h in hits if h.get("deliverable")]
    blocked = [h for h in hits if not h.get("deliverable")]
    best = deliverable[0] if deliverable else (hits[0] if hits else None)
    setup["delivery"] = {
        "would_deliver": bool(deliverable),
        "deliverable_count": len(deliverable),
        "blocked_count": len(blocked),
        "best_setup": best.get("setup") if best else None,
        "best_confluence": best.get("confluence") if best else None,
        "block_reason": None
        if deliverable
        else str(
            (best.get("gate_reason") if best else None)
            or (best.get("contract_issues") or ["no_bot_signal"])[0]
            if best
            else "no_bot_signal"
        ),
    }


def _delivery_audit(
    short_hits: list[dict[str, Any]], long_hits: list[dict[str, Any]]
) -> dict[str, Any]:
    def _summary(hits: list[dict[str, Any]]) -> dict[str, Any]:
        deliverable = [h for h in hits if h.get("deliverable")]
        blocked = [h for h in hits if not h.get("deliverable")]
        reasons: dict[str, int] = {}
        for h in blocked:
            key = str(h.get("gate_reason") or h.get("contract_issues") or "unknown")
            reasons[key] = reasons.get(key, 0) + 1
        top_blocked = sorted(reasons.items(), key=lambda x: -x[1])[:3]
        return {
            "total": len(hits),
            "deliverable": len(deliverable),
            "blocked": len(blocked),
            "top_block_reasons": top_blocked,
            "best_deliverable": deliverable[0] if deliverable else None,
        }

    return {
        "short": _summary(short_hits),
        "long": _summary(long_hits),
        "short_would_deliver": bool(any(h.get("deliverable") for h in short_hits)),
        "long_would_deliver": bool(any(h.get("deliverable") for h in long_hits)),
    }


def _phase_long(long_setup: dict[str, Any], confirmed: bool) -> str:
    if confirmed:
        return "long_confirmed"
    score = float(long_setup.get("long_score") or 0)
    if score >= 70:
        return "long_imminent"
    if score >= 45:
        return "long_setup_forming"
    if score >= 25:
        return "accumulation_watch"
    return "no_long_yet"


def _load_state() -> dict[str, str]:
    if not STATE_PATH.exists():
        return {}
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def _save_state(state: dict[str, str]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2), encoding="utf-8")


def _cooldown_ok(symbol: str, direction: str, state: dict[str, str], *, now: datetime) -> bool:
    key = f"{symbol}:{direction}"
    raw = state.get(key) or state.get(symbol)
    if not raw:
        return True
    try:
        last = datetime.fromisoformat(raw)
    except ValueError:
        return True
    return now - last >= timedelta(minutes=COOLDOWN_MINUTES)


def _fmt_price(value: float | None) -> str:
    if value is None:
        return "—"
    v = float(value)
    if abs(v) >= 100:
        return f"{v:.3f}"
    if abs(v) >= 1:
        return f"{v:.4f}"
    if abs(v) >= 0.01:
        return f"{v:.5f}"
    return f"{v:.6f}"


def _phase_badge(phase: str, confirmed: bool, *, direction: str = "short") -> str:
    if confirmed:
        return "🚨"
    if direction == "long":
        return {
            "long_imminent": "🟢",
            "long_setup_forming": "🟡",
            "long_confirmed": "🚨",
            "accumulation_watch": "🔵",
            "no_long_yet": "⚪",
        }.get(phase, "⚪")
    return {
        "dump_imminent": "🔴",
        "dump_setup_forming": "🟠",
        "dump_confirmed": "🚨",
        "exhaustion_watch": "🟡",
        "no_dump_yet": "⚪",
    }.get(phase, "⚪")


def _format_setup_lines(
    row: dict[str, Any],
    setup: dict[str, Any],
    *,
    direction: str,
    tf: dict[str, Any],
    pos: dict[str, Any],
    price: float,
) -> list[str]:
    score_key = "dump_score" if direction == "short" else "long_score"
    phase = str(setup.get("phase") or "—")
    confirmed = bool(setup.get("confirmed"))
    badge = _phase_badge(phase, confirmed, direction=direction)
    score = setup.get(score_key, "—")
    dir_label = "SHORT" if direction == "short" else "LONG"

    def _rsi(key: str) -> str:
        val = (tf.get(key) or {}).get("rsi14")
        return "—" if val is None else f"{val:.0f}"

    div_bits: list[str] = []
    if direction == "short":
        if (tf.get("1h") or {}).get("bearish_rsi_div"):
            div_bits.append("bear1h✓")
        if (tf.get("4h") or {}).get("bearish_rsi_div"):
            div_bits.append("bear4h✓")
    else:
        if (tf.get("1h") or {}).get("bullish_rsi_div"):
            div_bits.append("bull1h✓")
        if (tf.get("4h") or {}).get("bullish_rsi_div"):
            div_bits.append("bull4h✓")
    div_txt = " · " + " ".join(div_bits) if div_bits else ""

    triggers = setup.get("triggers") or []
    trig_txt = html.escape(", ".join(str(t) for t in triggers[:5]))
    if len(triggers) > 5:
        trig_txt += "…"

    hits = row.get("bot_short_hits" if direction == "short" else "bot_long_hits") or []
    top_hit = hits[0]["setup"] if hits else "—"
    hit_n = len(hits)

    ez = setup.get("entry_zone") or [price, price]
    oi = pos.get("oi")
    oi_chg = pos.get("oi_chg_5m")
    fund = pos.get("funding_pct")
    taker = pos.get("taker_5m")
    ls = pos.get("ls_5m")

    if direction == "short":
        level_line = (
            f"Support <code>{_fmt_price(setup.get('support_break_level'))}</code> · liq "
            f"<code>{_fmt_price(setup.get('resistance_liq'))}</code> · impulse H "
            f"<code>{_fmt_price(row.get('impulse_high'))}</code>"
        )
        bot_line = (
            f"Shorts bot: <code>{hit_n}</code> · top <code>{html.escape(str(top_hit))}</code>"
        )
    else:
        level_line = (
            f"Resistance <code>{_fmt_price(setup.get('resistance_break_level'))}</code> · support "
            f"<code>{_fmt_price(setup.get('support_zone'))}</code> · impulse L "
            f"<code>{_fmt_price(row.get('impulse_low'))}</code>"
        )
        bot_line = f"Longs bot: <code>{hit_n}</code> · top <code>{html.escape(str(top_hit))}</code>"

    lines = [
        f"{badge} <b>{dir_label}</b> · <code>{phase}</code> · score <code>{score}</code>",
        level_line,
        (
            f"Entry <code>{_fmt_price(ez[0])}-{_fmt_price(ez[1])}</code> · "
            f"SL <code>{_fmt_price(setup.get('stop_loss'))}</code> · "
            f"TP1 <code>{_fmt_price(setup.get('tp1'))}</code> · "
            f"TP2 <code>{_fmt_price(setup.get('tp2'))}</code>"
            + (
                f" · R:R <code>{setup.get('risk_reward')}</code>"
                if setup.get("risk_reward")
                else ""
            )
        ),
        (
            f"RSI 1m/5m/15m/1h/4h: "
            f"<code>{_rsi('1m')}/{_rsi('5m')}/{_rsi('15m')}/{_rsi('1h')}/{_rsi('4h')}</code>"
            f"{div_txt}"
        ),
        (
            f"OI <code>{_fmt_price(oi)}</code> · Δ5m <code>{oi_chg or '—'}</code> · "
            f"fund <code>{fund}%</code> · taker5m <code>{taker or '—'}</code> · "
            f"L/S <code>{ls or '—'}</code>"
        ),
        bot_line,
        f"Triggers: <code>{trig_txt or '—'}</code>",
    ]
    if confirmed:
        hard = setup.get("confirm_hard") or []
        lines.append(f"<b>✅ CONFIRM</b> {html.escape(', '.join(str(x) for x in hard))}")
    return lines


def _format_symbol_block(row: dict[str, Any]) -> str:
    if row.get("error"):
        sym = html.escape(str(row.get("symbol", "?")))
        return f"{sym}: <code>{html.escape(str(row['error']))}</code>"

    sym = html.escape(str(row["symbol"]).replace("USDT", "-USDT"))
    mode = str(row.get("watch_mode") or "short")
    price = float(row.get("price") or 0)
    chg = row.get("chg_24h_pct")
    vol = row.get("vol_24h_m")
    tf = row.get("timeframes") or {}
    pos = row.get("positioning") or {}

    header = (
        f"<b>{sym}</b> · {_mode_label(mode)} · "
        f"Цена <code>{_fmt_price(price)}</code> · 24h <code>{chg}%</code> · vol <code>{vol}M</code>"
    )
    sections: list[str] = [header]

    if row.get("dump") and mode in ("short", "both"):
        sections.extend(
            _format_setup_lines(row, row["dump"], direction="short", tf=tf, pos=pos, price=price)
        )
    if row.get("long") and mode in ("long", "both"):
        if mode == "both" and row.get("dump"):
            sections.append("—")
        sections.extend(
            _format_setup_lines(row, row["long"], direction="long", tf=tf, pos=pos, price=price)
        )
    return "\n".join(sections)


def _format_digest_telegram(rows: list[dict[str, Any]], *, tick_ts: datetime) -> str:
    ts = tick_ts.strftime("%H:%M UTC")
    blocks = [_format_symbol_block(row) for row in rows]
    body = "\n\n".join(blocks)
    header = (
        f"📊 <b>Signal Watch</b> · <code>{ts}</code>\n"
        f"<i>JCT·BEAT·VELVET dump · HYPE long · BTC any</i>"
    )
    footer = "<i>Signal-only. Confirm = отдельный алерт.</i>"
    return f"{header}\n\n{body}\n\n{footer}"


def _format_telegram(row: dict[str, Any], *, direction: str, confirm_reasons: list[str]) -> str:
    sym = html.escape(str(row["symbol"]).replace("USDT", "-USDT"))
    setup = row["dump"] if direction == "short" else row["long"]
    score_key = "dump_score" if direction == "short" else "long_score"
    price = float(row.get("price") or 0)
    tf = row.get("timeframes") or {}
    pos = row.get("positioning") or {}
    badge = "🔴" if direction == "short" else "🟢"
    label = "DUMP SHORT" if direction == "short" else "LONG"
    header = (
        f"{badge} <b>{label} {sym}</b> · <b>CONFIRMED</b>\n"
        f"Score <code>{setup.get(score_key)}</code> · phase <code>{setup.get('phase')}</code> · "
        f"price <code>{_fmt_price(price)}</code> · 24h <code>{row.get('chg_24h_pct')}%</code>"
    )
    body = "\n".join(
        _format_setup_lines(row, setup, direction=direction, tf=tf, pos=pos, price=price)
    )
    regime = row.get("regime") or {}
    regime_line = (
        f"Regime 4h/1h: <code>{regime.get('regime_4h')}/{regime.get('regime_1h')}</code> · "
        f"structure <code>{regime.get('structure_1h')}</code>"
    )
    confirm = f"<b>Confirm</b> {html.escape(', '.join(confirm_reasons))}"
    delivery = setup.get("delivery") or {}
    delivery_line = (
        f"✅ <b>Delivery OK</b> · bot setup <code>{html.escape(str(delivery.get('best_setup') or '—'))}</code> · "
        f"conf <code>{delivery.get('best_confluence')}</code>"
        if delivery.get("would_deliver")
        else (
            f"⚠️ <b>Main-bot blocked</b> <code>{html.escape(str(delivery.get('block_reason') or 'unknown'))}</code> · "
            f"<i>hunt confirm still valid</i>"
        )
    )
    footer = "<i>Signal-only · closed 5m/1m confirm · открывай сделку вручную.</i>"
    return f"{header}\n{confirm}\n{regime_line}\n{delivery_line}\n\n{body}\n\n{footer}"


def _format_followup_telegram(followup: Any, row: dict[str, Any]) -> str:
    sym = html.escape(str(followup.symbol).replace("USDT", "-USDT"))
    direction = followup.direction.upper()
    price = _fmt_price(followup.price)
    lc = row.get("lifecycle") or {}
    badges = {
        "invalidate": "⛔",
        "fix_profit_tp1": "💰",
        "fix_profit_tp2": "💰",
        "phase_change": "🔄",
        "stop_warning": "⚠️",
        "avg_zone": "➕",
    }
    titles = {
        "invalidate": "SIGNAL OFF — фиксируй / не добавляй",
        "fix_profit_tp1": "FIX PROFIT — TP1 зона",
        "fix_profit_tp2": "FIX PROFIT — TP2 / закрыть",
        "phase_change": "PHASE CHANGE",
        "stop_warning": "STOP WARNING — у SL",
        "avg_zone": "AVG ZONE",
    }
    badge = badges.get(followup.event, "📣")
    title = titles.get(followup.event, followup.event)
    setup = row.get("dump") if followup.direction == "short" else row.get("long")
    sl = _fmt_price((setup or {}).get("stop_loss"))
    tp1 = _fmt_price((setup or {}).get("tp1"))
    return (
        f"{badge} <b>{title}</b>\n"
        f"{sym} · <code>{direction}</code> · price <code>{price}</code>\n"
        f"<code>{html.escape(followup.detail)}</code>\n"
        f"Lifecycle <code>{html.escape(str(lc.get('phase') or '—'))}</code> · "
        f"SL <code>{sl}</code> · TP1 <code>{tp1}</code>\n"
        f"<i>Hunt follow-up · не auto-trade</i>"
    )


def _split_telegram(text: str, *, limit: int = 3900) -> list[str]:
    if len(text) <= limit:
        return [text]
    parts: list[str] = []
    chunk = ""
    for block in text.split("\n\n"):
        candidate = f"{chunk}\n\n{block}".strip() if chunk else block
        if len(candidate) <= limit:
            chunk = candidate
            continue
        if chunk:
            parts.append(chunk)
        chunk = block
    if chunk:
        parts.append(chunk)
    return parts or [text[:limit]]


async def _send_telegram_chunks(
    broadcaster: TelegramBroadcaster,
    text: str,
    *,
    log_key: str,
) -> bool:
    ok = True
    for idx, part in enumerate(_split_telegram(text)):
        result = await broadcaster.send_html(part)
        if result.status != "sent":
            LOG.warning(
                f"{log_key}_failed",
                part=idx + 1,
                status=result.status,
                reason=result.reason,
            )
            ok = False
        else:
            LOG.info(f"{log_key}_sent", part=idx + 1, message_id=result.message_id)
    return ok


async def _snapshot_symbol(
    client: BinanceFuturesMarketData,
    settings: Any,
    minimums: dict[str, int],
    symbol: str,
    *,
    watch_mode: WatchMode,
    prev_oi: float | None,
    engine: SignalEngine,
    conf_eng: ConfluenceEngine,
    premium_all: dict[str, dict[str, float]],
    funding_info_all: dict[str, dict[str, float | int]],
    btc_work_1h: Any | None,
    exchange_by_sym: dict[str, Any],
    ticker_by_sym: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    meta = exchange_by_sym.get(symbol)
    ticker = ticker_by_sym.get(symbol)
    if meta is None or ticker is None:
        return {
            "ts": datetime.now(UTC).isoformat(),
            "symbol": symbol,
            "error": f"symbol_meta_or_ticker_missing:{symbol}",
        }
    price = float(ticker.get("last_price") or 0)
    market_row = {
        "symbol": symbol,
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
        symbol=symbol,
        base_asset=meta.base_asset,
        quote_asset=meta.quote_asset,
        contract_type=meta.contract_type,
        status=meta.status,
        onboard_date_ms=meta.onboard_date_ms,
        quote_volume=market_row["quote_volume"],
        price_change_pct=market_row["price_change_percent"],
        last_price=price,
        shortlist_bucket="dump_watch",
        seed_source="dump_minute_watch",
        strategy_fits=strategy_fits_for_market_row(market_row, settings=settings),
    )
    limits = _kline_limits(minimums)
    kline_tasks = {
        "1m": client.fetch_klines_cached(symbol, "1m", limit=limits["1m"]),
        "3m": client.fetch_klines_cached(symbol, "3m", limit=limits["3m"]),
        "5m": client.fetch_klines_cached(symbol, "5m", limit=limits["5m"]),
        "15m": client.fetch_klines_cached(symbol, "15m", limit=limits["15m"]),
        "1h": client.fetch_klines_cached(symbol, "1h", limit=limits["1h"]),
        "4h": client.fetch_klines_cached(symbol, "4h", limit=limits["4h"]),
        "1d": client.fetch_klines_cached(symbol, "1d", limit=limits["1d"]),
    }
    kline_results = await asyncio.gather(*kline_tasks.values(), return_exceptions=True)
    kline_map: dict[str, Any] = {}
    for name, res in zip(kline_tasks.keys(), kline_results, strict=True):
        kline_map[name] = None if isinstance(res, BaseException) else res
    df_1m = kline_map["1m"]
    if df_1m is None or df_1m.is_empty():
        return {"ts": datetime.now(UTC).isoformat(), "symbol": symbol, "error": "klines_1m_failed"}
    df_5m = kline_map["5m"]
    pack = await _fetch_rest_pack(client, symbol)
    book = _book_from_pack(pack)
    frames = SymbolFrames(
        symbol=symbol,
        df_15m=kline_map["15m"],
        df_1h=kline_map["1h"],
        df_5m=df_5m,
        df_4h=kline_map["4h"],
        bid_price=book.get("bid_price"),
        ask_price=book.get("ask_price"),
        bid_qty=book.get("bid_qty"),
        ask_qty=book.get("ask_qty"),
        frame_source_flags=("frames_rest_full",),
    )

    prepared = prepare_symbol(item, frames, minimums=minimums, settings=settings)
    if prepared is None:
        return {"ts": datetime.now(UTC).isoformat(), "symbol": symbol, "error": "prepare_failed"}

    work_1m = _prepare_frame(df_1m)
    work_3m = _prepare_frame(kline_map["3m"]) if kline_map.get("3m") is not None else None
    delta_raw = None
    if prepared.work_15m is not None and not prepared.work_15m.is_empty():
        delta_raw = _col(prepared.work_15m, "delta_ratio", None)
    delta = None if delta_raw is None else float(delta_raw)
    premium_row = premium_all.get(symbol) or premium_all.get(symbol.upper())
    funding_info = funding_info_all.get(symbol) or funding_info_all.get(symbol.upper())
    _apply_rest_enrichments(
        prepared,
        client=client,
        symbol=symbol,
        pack=pack,
        book=book,
        premium_row=premium_row,
        funding_info=funding_info,
        delta=delta,
    )
    if symbol != "BTCUSDT" and btc_work_1h is not None:
        corr = _btc_corr_1h(prepared.work_1h, btc_work_1h)
        if corr is not None:
            prepared.btc_corr_1h = corr

    impulse = _impulse_context(prepared.work_4h, prepared.work_1h, symbol)
    ih4, il4 = impulse["impulse_high_4h"], impulse["impulse_low_4h"]
    hunt_h, hunt_l = impulse["hunt_high"], impulse["hunt_low"]
    fib_4h = _fib_levels(ih4, il4)
    fib_hunt = _fib_levels(hunt_h, hunt_l)
    fib = {**fib_4h, "hunt": fib_hunt}
    session = _session_stats(work_1m)

    work_1d_snap = _prepare_frame(kline_map["1d"]) if kline_map.get("1d") is not None else None
    if work_1d_snap is not None and not work_1d_snap.is_empty():
        probe = _tf_snapshot(work_1d_snap)
        tf_1d = (
            probe
            if probe.get("status") != "empty" and probe.get("rsi14") is not None
            else _tf_snapshot_lite(kline_map["1d"])
        )
    elif kline_map.get("1d") is not None:
        tf_1d = _tf_snapshot_lite(kline_map["1d"])
    else:
        tf_1d = {"status": "empty"}

    tf = {
        "1m": _tf_snapshot(work_1m),
        "1m_closed": _tf_snapshot(work_1m, closed=True),
        "3m": _tf_snapshot(work_3m) if work_3m is not None else {"status": "empty"},
        "3m_closed": _tf_snapshot(work_3m, closed=True)
        if work_3m is not None
        else {"status": "empty"},
        "5m": _tf_snapshot(prepared.work_5m),
        "5m_closed": _tf_snapshot(prepared.work_5m, closed=True),
        "15m": _tf_snapshot(prepared.work_15m),
        "15m_closed": _tf_snapshot(prepared.work_15m, closed=True),
        "1h": _tf_snapshot(prepared.work_1h),
        "1h_closed": _tf_snapshot(prepared.work_1h, closed=True),
        "4h": _tf_snapshot(prepared.work_4h),
        "1d": tf_1d,
    }
    market = _market_snapshot(
        prepared, pack=pack, book=book, premium_row=premium_row, ticker=ticker
    )
    regime = _regime_snapshot(prepared)
    short_hits: list[dict[str, Any]] = []
    long_hits: list[dict[str, Any]] = []
    for r in await engine.calculate_all(prepared, event_interval="15m"):
        if r.signal is None:
            continue
        delivery = _evaluate_delivery(r.signal, prepared, settings=settings, conf_eng=conf_eng)
        hit = {
            "setup": r.signal.setup_id,
            "score": round(float(r.signal.score), 3),
            "stop": r.signal.stop,
            "tp1": r.signal.take_profit_1,
            **delivery,
        }
        if r.signal.direction == "short":
            short_hits.append(hit)
        elif r.signal.direction == "long":
            long_hits.append(hit)

    delivery_audit = _delivery_audit(short_hits, long_hits)

    result: dict[str, Any] = {
        "ts": datetime.now(UTC).isoformat(),
        "symbol": symbol,
        "watch_mode": watch_mode,
        "price": price,
        "chg_24h_pct": round(float(ticker.get("price_change_percent") or 0), 2),
        "vol_24h_m": market.get("vol_24h_m"),
        "market": market,
        "positioning": market,
        "regime": regime,
        "timeframes": tf,
        "session": session,
        "impulse": impulse,
        "impulse_high": hunt_h,
        "impulse_low": hunt_l,
        "fib": fib,
        "kline_limits": limits,
        "bot_short_hits": short_hits,
        "bot_long_hits": long_hits,
        "delivery_audit": delivery_audit,
        "data_quality": _data_quality_report(
            prepared,
            frames=frames,
            df_1m=df_1m,
            pack=pack,
            book=book,
            tf=tf,
        ),
    }

    lifecycle = assess_hunt_lifecycle(
        price=price,
        hunt_high=hunt_h,
        hunt_low=hunt_l,
        session=session,
        tf=tf,
        market=market,
    )
    lifecycle_dict = {
        "phase": lifecycle.phase.value,
        "recommended_bias": lifecycle.recommended_bias,
        "short_entry_ok": lifecycle.short_entry_ok,
        "invalidate_short": lifecycle.invalidate_short,
        "fall_from_high_pct": lifecycle.fall_from_high_pct,
        "bounce_from_low_pct": lifecycle.bounce_from_low_pct,
        "local_support": lifecycle.local_support,
        "local_resistance": lifecycle.local_resistance,
        "reasons": list(lifecycle.reasons),
    }
    result["lifecycle"] = lifecycle_dict

    if watch_mode in ("short", "both"):
        support_level = effective_support_break(impulse_high=hunt_h, lifecycle=lifecycle)
        dump = _dump_analysis(
            price=price,
            tf=tf,
            market=market,
            regime=regime,
            impulse_high=hunt_h,
            impulse_low=hunt_l,
            support_break_level=support_level,
            fib=fib_hunt,
            short_hits=short_hits,
            prev_oi=prev_oi,
            cur_oi=prepared.oi_current,
            local_support=lifecycle.local_support,
            local_resistance=lifecycle.local_resistance,
        )
        confirmed, confirm_hard = _confirm_dump(dump, tf)
        confirmed, confirm_hard, lifecycle_note = apply_short_invalidation(
            confirmed,
            confirm_hard,
            lifecycle,
            dump=dump,
        )
        dump["phase"] = _phase(dump, confirmed, lifecycle_note=lifecycle_note)
        dump["confirmed"] = confirmed
        dump["confirm_hard"] = confirm_hard
        dump["lifecycle"] = lifecycle_dict
        if lifecycle_note:
            dump["lifecycle_note"] = lifecycle_note
        _attach_delivery_meta(dump, short_hits)
        result["dump"] = dump

    if watch_mode in ("long", "both"):
        long_setup = _long_analysis(
            price=price,
            tf=tf,
            market=market,
            regime=regime,
            impulse_low=hunt_l,
            impulse_high=hunt_h,
            fib=fib_hunt,
            long_hits=long_hits,
            prev_oi=prev_oi,
            cur_oi=prepared.oi_current,
        )
        long_confirmed, long_hard = _confirm_long(long_setup, tf)
        long_setup["phase"] = _phase_long(long_setup, long_confirmed)
        long_setup["confirmed"] = long_confirmed
        long_setup["confirm_hard"] = long_hard
        _attach_delivery_meta(long_setup, long_hits)
        result["long"] = long_setup

    return result


async def _run_tick(
    symbols: tuple[str, ...],
    *,
    mode_map: dict[str, WatchMode],
    broadcaster: TelegramBroadcaster | None,
    send_telegram: bool,
) -> list[dict[str, Any]]:
    settings = load_settings()
    minimums = min_required_bars(
        min_bars_15m=settings.filters.min_bars_15m,
        min_bars_1h=settings.filters.min_bars_1h,
        min_bars_4h=settings.filters.min_bars_4h,
    )
    registry = StrategyRegistry()
    for cls in STRATEGY_CLASSES:
        registry.register(cls(SetupParams(enabled=True), settings))
    engine = SignalEngine(registry, settings)
    conf_eng = ConfluenceEngine(settings)

    client = BinanceFuturesMarketData(
        binance_client=BinanceClientImpl(
            rest_timeout_seconds=45.0,
            futures_data_request_limit_per_5m=settings.runtime.futures_data_request_limit_per_5m,
            proxy_url=settings.network.proxy_url,
            trust_env=settings.network.trust_env,
        ),
    )
    prev_oi: dict[str, float | None] = dict.fromkeys(symbols)
    state = _load_state()
    tracker_state = load_tracker_state()
    now = datetime.now(UTC)
    rows: list[dict[str, Any]] = []
    try:
        premium_all = await _safe_fetch(client.fetch_premium_index_all()) or {}
        funding_info_all = await _safe_fetch(client.fetch_funding_info_all()) or {}
        exchange_list = await _safe_fetch(client.fetch_exchange_symbols()) or []
        exchange_by_sym = {r.symbol: r for r in exchange_list}
        ticker_raw = await _safe_fetch(client.fetch_ticker_24h()) or []
        ticker_by_sym = {str(t.get("symbol")): t for t in ticker_raw if t.get("symbol")}
        btc_work_1h: Any | None = None
        if any(s != "BTCUSDT" for s in symbols):
            btc_df = await _safe_fetch(client.fetch_klines_cached("BTCUSDT", "1h", limit=500))
            if btc_df is not None and not btc_df.is_empty():
                btc_work_1h = _prepare_frame(btc_df)

        for symbol in symbols:
            try:
                mode = effective_watch_mode(
                    symbol,
                    mode_map,
                    lifecycle_bias=None,
                )
                row = await asyncio.wait_for(
                    _snapshot_symbol(
                        client,
                        settings,
                        minimums,
                        symbol,
                        watch_mode=mode,
                        prev_oi=prev_oi.get(symbol),
                        engine=engine,
                        conf_eng=conf_eng,
                        premium_all=premium_all,
                        funding_info_all=funding_info_all,
                        btc_work_1h=btc_work_1h,
                        exchange_by_sym=exchange_by_sym,
                        ticker_by_sym=ticker_by_sym,
                    ),
                    timeout=SYMBOL_TICK_TIMEOUT_S,
                )
                oi_val = (row.get("market") or row.get("positioning") or {}).get("oi")
                if oi_val is not None:
                    prev_oi[symbol] = float(oi_val)
                rows.append(row)
                dump = row.get("dump") or {}
                long_setup = row.get("long") or {}
                lifecycle_raw = row.get("lifecycle") or (dump.get("lifecycle") if dump else None)
                if lifecycle_raw and isinstance(lifecycle_raw, dict):
                    mode = effective_watch_mode(
                        symbol,
                        mode_map,
                        lifecycle_bias=str(lifecycle_raw.get("recommended_bias") or ""),
                    )
                    row["watch_mode"] = mode
                LOG.info(
                    "watch_tick",
                    symbol=symbol,
                    mode=mode,
                    price=row.get("price"),
                    hunt_phase=(lifecycle_raw or {}).get("phase"),
                    short_score=dump.get("dump_score"),
                    short_phase=dump.get("phase"),
                    short_confirmed=dump.get("confirmed"),
                    long_score=long_setup.get("long_score"),
                    long_phase=long_setup.get("phase"),
                    long_confirmed=long_setup.get("confirmed"),
                    short_deliver=dump.get("delivery", {}).get("would_deliver"),
                    long_deliver=long_setup.get("delivery", {}).get("would_deliver"),
                    data_missing=(row.get("data_quality") or {}).get("fields_missing"),
                )
                if send_telegram and broadcaster is not None:
                    followups = evaluate_followups(tracker_state, row, now=now)
                    for fu in followups:
                        msg = _format_followup_telegram(fu, row)
                        result = await broadcaster.send_html(msg)
                        if result.status == "sent":
                            LOG.info(
                                "watch_followup_sent",
                                symbol=fu.symbol,
                                event=fu.event,
                                message_id=result.message_id,
                            )
                    if followups:
                        mark_followups_sent(tracker_state, followups, now=now)

                    for direction, setup in (("short", dump), ("long", long_setup)):
                        if not setup:
                            continue
                        if not _should_alert(
                            setup,
                            direction=direction,
                            symbol=symbol,
                            lifecycle=lifecycle_raw,
                            row=row,
                        ):
                            lc = lifecycle_raw if isinstance(lifecycle_raw, dict) else {}
                            if bool(setup.get("confirmed")):
                                LOG.info(
                                    "watch_alert_blocked",
                                    symbol=symbol,
                                    direction=direction,
                                    score=setup.get("dump_score") or setup.get("long_score"),
                                    hunt_phase=lc.get("phase"),
                                )
                            continue
                        if direction == "short" and mode not in ("short", "both"):
                            continue
                        if direction == "long" and mode not in ("long", "both"):
                            continue
                        if not _cooldown_ok(symbol, direction, state, now=now):
                            continue
                        msg = _format_telegram(
                            row,
                            direction=direction,
                            confirm_reasons=setup.get("confirm_hard") or [],
                        )
                        result = await broadcaster.send_html(msg)
                        key = f"{symbol}:{direction}"
                        if result.status == "sent":
                            state[key] = now.isoformat()
                            setup_latch = {**setup, "telegram_sent": True}
                            register_signal_open(
                                tracker_state,
                                symbol=symbol,
                                direction=direction,
                                price=float(row.get("price") or 0),
                                setup=setup_latch,
                                lifecycle=lifecycle_raw
                                if isinstance(lifecycle_raw, dict)
                                else None,
                                now=now,
                            )
                            LOG.info(
                                "watch_telegram_sent",
                                symbol=symbol,
                                direction=direction,
                                message_id=result.message_id,
                            )
                        else:
                            LOG.warning(
                                "watch_telegram_failed",
                                symbol=symbol,
                                direction=direction,
                                status=result.status,
                                reason=result.reason,
                            )
            except TimeoutError:
                LOG.warning("watch_symbol_timeout", symbol=symbol, timeout_s=SYMBOL_TICK_TIMEOUT_S)
                rows.append(
                    {"ts": now.isoformat(), "symbol": symbol, "error": "symbol_tick_timeout"}
                )
            except DEFENSIVE_EXC as exc:
                LOG.warning("dump_symbol_failed", symbol=symbol, error=repr(exc))
                rows.append({"ts": now.isoformat(), "symbol": symbol, "error": repr(exc)})
        _save_state(state)
        save_tracker_state(tracker_state)
        return rows
    finally:
        await client.close()


async def _run_loop(
    cli_symbols: tuple[str, ...],
    interval_s: int,
    once: bool,
    *,
    send_telegram: bool,
) -> None:
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    settings = load_settings()
    broadcaster: TelegramBroadcaster | None = None
    if send_telegram:
        if not settings.tg_token or not settings.target_chat_id:
            msg = "Telegram not configured (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID)"
            raise RuntimeError(msg)
        for attempt in range(3):
            try:
                broadcaster = TelegramBroadcaster(settings.tg_token, settings.target_chat_id)
                await broadcaster.preflight_check()
                LOG.info("watch_telegram_ready", chat=settings.target_chat_id, mode="confirm_only")
                break
            except DEFENSIVE_EXC as exc:
                LOG.warning("watch_telegram_preflight_failed", attempt=attempt + 1, error=repr(exc))
                broadcaster = None
                if attempt < 2:
                    await asyncio.sleep(2.0 * (attempt + 1))
        if broadcaster is None:
            LOG.warning("watch_telegram_disabled", reason="preflight_failed")
            send_telegram = False

    last_scan = 0.0
    while not _STOP:
        started = time.monotonic()
        try:
            if time.monotonic() - last_scan >= SCAN_INTERVAL_S:
                try:
                    from hunt_watch.scanner_runner import run_scan

                    summary = await run_scan(limit=30, min_score=45.0)
                    LOG.info(
                        "hunt_scan_refresh",
                        watch=summary.get("watch_count"),
                        priority=summary.get("priority_count"),
                    )
                except DEFENSIVE_EXC as exc:
                    LOG.warning("hunt_scan_refresh_failed", error=repr(exc))
                last_scan = time.monotonic()

            settings = load_settings()
            symbols, mode_map = resolve_watch_universe(settings, static_modes=SYMBOL_WATCH_MODES)
            merged: list[str] = list(symbols)
            for sym in cli_symbols:
                s = sym.upper()
                if s not in merged:
                    merged.append(s)
                mode_map.setdefault(s, SYMBOL_WATCH_MODES.get(s, "short"))
            active = tuple(dict.fromkeys(merged))
            LOG.info("watch_universe", symbols=len(active), list=list(active)[:8])

            rows = await _run_tick(
                active,
                mode_map=mode_map,
                broadcaster=broadcaster,
                send_telegram=send_telegram,
            )
            with OUT_PATH.open("a", encoding="utf-8") as fh:
                for row in rows:
                    fh.write(json.dumps(row, default=str) + "\n")
            if once:
                print(json.dumps(rows, indent=2, default=str))
                break
        except Exception:
            LOG.exception("dump_watch_tick_error")
            if once:
                raise
        if once:
            break
        elapsed = time.monotonic() - started
        await asyncio.sleep(max(1.0, interval_s - elapsed))

    if broadcaster is not None:
        await broadcaster.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Signal minute watch + Telegram")
    parser.add_argument(
        "--symbols",
        nargs="*",
        default=list(DEFAULT_SYMBOLS),
        help="Default: JCT BEAT (short), VELVET HYPE (long/pump), BTC (both)",
    )
    parser.add_argument("--interval", type=int, default=60)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--no-telegram", action="store_true", help="Log only, no Telegram sends")
    args = parser.parse_args()
    symbols = tuple(dict.fromkeys(s.upper() for s in args.symbols))
    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)
    asyncio.run(
        _run_loop(
            symbols,
            args.interval,
            args.once,
            send_telegram=not args.no_telegram,
        )
    )


if __name__ == "__main__":
    main()
