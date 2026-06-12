#!/usr/bin/env python3
"""Multi-symbol pump/dump hunt — REST + live WS (liq/aggTrade) + spot lead-lag.

Hunt uses engine/ data plane: minute REST poll for klines/OI/L-S, background WS for
liquidation cascades and sub-minute orderflow, spot companion for lead-lag vs perp.
"""

from __future__ import annotations

import argparse
import asyncio
import html
import json
import os
import signal
import time
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from hunt_watch.bootstrap import bootstrap

bootstrap()

from hunt_watch.adaptive_thresholds import load_adaptive_store, save_adaptive_store
from hunt_watch.data_completeness import DataIncompleteError, series_z_strict
from hunt_watch.ignition import (
    format_ignition_telegram,
    load_ignition_state,
    mark_ignition_notified,
    pending_ignition_alerts,
    process_ticker_snapshots,
    save_ignition_state,
)
from hunt_watch.indicators import rsi14_from_ohlc
from hunt_watch.directional_filters import directional_filters
from hunt_watch.dump_hunt_alert import (
    dump_hunt_skip_reason,
    format_dump_hunt_telegram,
    maybe_send_dump_hunt_telegram,
)
from hunt_watch.feature_latch import (
    book_walls_from_depth,
    book_walls_from_row,
    feature_vector_from_row,
)
from hunt_watch.frame_fallback import patch_work_4h, should_use_young_lite_path
from hunt_watch.levels import fib_retracement_levels, structural_long_levels, structural_short_levels
from hunt_watch.lifecycle import (
    apply_short_invalidation,
    assess_hunt_lifecycle,
    blocks_premature_exhaustion_short,
    effective_support_break,
    promote_initial_pump_lifecycle,
)
from hunt_watch.lifecycle_sticky import stabilize as stabilize_lifecycle
from hunt_watch.market_regime import (
    REGIME_REFRESH_S,
    active_params,
    apply_snapshot,
    load_regime_file,
    refresh_market_regime,
)
from hunt_watch.alert_explain import evaluate_alert_gate, invalidate_detail_human
from hunt_watch.early_alert import (
    EARLY_TELEGRAM_ENABLED,
    evaluate_early_alert,
    early_cooldown_ok,
    early_telegram_enabled,
    format_early_telegram,
    mark_early_sent,
)
from hunt_watch.param_store import effective_hunt_params, migrate_calibration_split
from hunt_watch.paths import TELEGRAM_COOLDOWN, TICK_JSONL
from hunt_watch.session_state import merge_hunt_extremes
from hunt_watch.signal_events import append_signal_event
from hunt_watch.tick_rotate import rotate_hunt_ticks
from hunt_watch.telegram_commands import build_hunt_telegram_commands
from hunt_watch.watchlist_ops import clear_signal_notify, load_pending_notify
from hunt_watch.pump_history import (
    backfill_from_jsonl,
    format_history_telegram,
    load_pump_history,
    observe_prices,
    record_pump_leg,
    record_signal_outcome,
    save_pump_history,
    score_bonus,
    stats_for,
)
from hunt_watch.pump_history import (
    record_signal_open as record_pump_signal_open,
)
from hunt_watch.prep_shadow_tracker import (
    load_prep_shadow_state,
    process_prep_shadow,
    save_prep_shadow_state,
)
from hunt_watch.scriptutil import configure_script_logging
from hunt_watch.signal_engine import (
    confirm_dump as _se_confirm_dump,
    confirm_long as _se_confirm_long,
    enrich_dump_setup,
    enrich_long_setup,
    phase_dump as _se_phase_dump,
    phase_long as _se_phase_long,
)
from hunt_watch.signal_tracker import (
    evaluate_followups,
    latch_row_setups,
    load_tracker_state,
    mark_followups_sent,
    reconcile_signal,
    register_signal_open,
    save_tracker_state,
)
from hunt_watch.targets import (
    DEFAULT_MODES,
    DEFAULT_SYMBOLS,
    PINNED_SYMBOLS,
    effective_watch_mode,
    resolve_watch_universe,
)
from hunt_watch.liquidity_gate import liquidity_skip_reason
from hunt_watch.ws_feed import HuntWsFeed

from engine.data_readiness import kline_fetch_limit
from engine.domain.config import load_settings
from engine.domain.schemas import SymbolFrames, UniverseSymbol
from engine.errors import DEFENSIVE_EXC, defensive_exc_types
from engine.features.pivots import _pivot_rows, with_spec_columns
from engine.features.prepare import _prepare_frame, min_required_bars, prepare_symbol
from engine.market._ws_parsers import depth_imbalance_from_book, microprice_bias_from_book
from engine.market.data import BinanceFuturesMarketData
from engine.market.rest_impl import BinanceClientImpl
from engine.market.spot_companion import SpotCompanionService
from engine.telegram import TelegramBroadcaster

WatchMode = Literal["short", "long", "both"]

SYMBOL_WATCH_MODES: dict[str, WatchMode] = dict(DEFAULT_MODES)
# 4h swing window (bars) — majors/metals; dynamic alts use shorter window via scanner.
IMPULSE_WINDOW: dict[str, int] = {
    "BTCUSDT": 30,
    "ETHUSDT": 30,
    "XAUUSDT": 24,
    "XAGUSDT": 24,
}
IMPULSE_WINDOW_1H: dict[str, int] = {
    "BTCUSDT": 168,
    "ETHUSDT": 120,
    "XAUUSDT": 72,
    "XAGUSDT": 72,
}
# Dynamic memecoins from scanner: 1h impulse leg (faster pump/dump structure).
FAST_IMPULSE_SYMBOLS = frozenset()  # populated per-tick for non-pinned watchlist names
IMPULSE_WINDOW_ALT_4H = 12
IMPULSE_WINDOW_ALT_1H = 48
SYMBOL_TICK_TIMEOUT_S = 180


def _kline_limits(minimums: dict[str, int]) -> dict[str, int]:
    """Hunt watch pulls deeper history than default bot warmup (max 1500 bars)."""
    return {
        "1m": min(1500, max(1440, kline_fetch_limit(int(minimums.get("5m", 300)), "5m") * 2)),
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
    use_1h = sym not in PINNED_SYMBOLS
    if use_1h:
        ih4, il4 = _swing_range(work_4h, window=IMPULSE_WINDOW_ALT_4H)
        ih1, il1 = _swing_range(work_1h, window=IMPULSE_WINDOW_ALT_1H)
    else:
        ih4, il4 = _swing_range(work_4h, window=IMPULSE_WINDOW.get(sym, 30))
        ih1, il1 = _swing_range(work_1h, window=IMPULSE_WINDOW_1H.get(sym, 120))
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
TICK_ROTATE_INTERVAL_S = 600
TICK_ROTATE_MIN_BYTES = 65_536
COOLDOWN_MINUTES = 45
# Score / R:R floors default here; runtime overrides via market_regime.active_params().
FORMING_MIN_SCORE = 45
MIN_RISK_REWARD = 1.0
HUNT_MIN_RISK_REWARD = 0.8
BOUNCE_MIN_RISK_REWARD = 0.5

# Ignition: fast pump/dump start detection from per-tick 24h-ticker deltas (no extra API calls).
IGNITION_WINDOW_S = 300
IGNITION_MIN_PCT = 2.5
IGNITION_MIN_VOL_DELTA_USD = 250_000.0
IGNITION_MIN_QVOL_USD = 3_000_000.0
IGNITION_TTL_S = 7200.0  # ignited symbols stay in minute-watch this long
# Ignition → watchlist only; TG alerts were noisy spam (user request 2026-06-10).
IGNITION_TELEGRAM_ENABLED = False

# H-A "sniper" product (Gate G2, 2026-06-12): live TG delivery is restricted to the
# only data-validated edge — short fade confirmed in lifecycle phase `dump_active`
# (hold-to-target backtest: 19% SL / 68% TP1 on n=37 vs 52% raw baseline). Long setups
# are still computed and tracked (shadow / research-only) but never delivered to TG;
# their followups stay silent via the existing `announced` gate. Set 0 to disable.
HUNT_SNIPER_MODE = os.environ.get("HUNT_SNIPER_MODE", "1") not in {"0", "false", "False"}
HUNT_SNIPER_LIVE_PHASES = frozenset({"dump_active"})
# HMSTR-class squeeze guard: block live short when top-trader L/S ratio is this high
# (data-grounded on 11 live signals — losers HMSTR 2.48 / EPIC 2.11 vs winners <=1.91).
HUNT_SNIPER_TOP_LS_MAX = float(os.environ.get("HUNT_SNIPER_TOP_LS_MAX", "2.0"))

LOG = configure_script_logging("scripts.dump_minute_watch")
_STOP = False


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
        # Populates _funding_history_cache (client-cached 900s) — without this
        # funding_trend / funding_zscore_48h were null in 100% of telemetry:
        # the cached getters had no producer.
        ("funding_hist", client.fetch_funding_rate_history(symbol, limit=16)),
        ("basis_5m", client.fetch_basis(symbol, period="5m")),
        ("agg_trades", client.fetch_agg_trade_snapshot(symbol, limit=100)),
        ("book_depth", client.fetch_order_book_depth_snapshot(symbol, limit=100)),
        # Series (4h of 5m points, client-cached 240s): z-score baseline for
        # OI build/flush and positioning extremes — squeeze/cascade fuel.
        ("oi_series", client.fetch_open_interest_series(symbol, period="5m", limit=48)),
        ("gls_series", client.fetch_global_ls_series(symbol, period="5m", limit=48)),
    ]
    results = await asyncio.gather(*(c for _, c in specs), return_exceptions=True)
    pack: dict[str, Any] = {}
    for (name, _), res in zip(specs, results, strict=True):
        pack[name] = None if isinstance(res, BaseException) else res
    depth = pack.get("book_depth")
    if not isinstance(depth, dict) or not depth.get("bid_price"):
        pack["book_ticker"] = await _safe_fetch(client._fetch_book_ticker_rest_detail(symbol))
    return pack


def _series_z(values: Any) -> float | None:
    """Z-score of the LAST point vs the prior window — single implementation
    lives in data_completeness.series_z_strict (ddof=1); None on bad data."""
    if not isinstance(values, list) or len(values) < 12:
        return None
    try:
        return round(series_z_strict([float(x) for x in values], field="series"), 2)
    except (DataIncompleteError, TypeError, ValueError):
        return None


def _series_chg_pct(values: Any) -> float | None:
    """Percent change over the whole series window (first -> last)."""
    if not isinstance(values, list) or len(values) < 2:
        return None
    first = float(values[0])
    if first == 0:
        return None
    return round((float(values[-1]) / first - 1.0) * 100.0, 2)


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


def _overlay_ws_market(prepared: Any, ws_snap: dict[str, Any] | None) -> None:
    """Prefer live WS orderflow + mark/ap between REST polls (reports A7/A8)."""
    if not ws_snap:
        return
    ws_delta = ws_snap.get("agg_trade_delta_30s")
    if ws_delta is not None:
        prepared.agg_trade_delta_30s = float(ws_delta)
        prepared.orderflow_source = str(ws_snap.get("agg_trade_source") or "ws_nq")
    if ws_snap.get("funding_live") is not None:
        prepared.funding_rate = float(ws_snap["funding_live"])
    if ws_snap.get("mark_live") is not None:
        prepared.mark_price = float(ws_snap["mark_live"])
    if ws_snap.get("basis_bps_live") is not None:
        bps = float(ws_snap["basis_bps_live"])
        prepared.basis_pct = bps / 100.0
        prepared.mark_index_spread_bps = bps
    if ws_snap.get("basis_ap_bps") is not None:
        ap_bps = float(ws_snap["basis_ap_bps"])
        prepared.basis_pct = ap_bps / 100.0
        prepared.mark_index_spread_bps = ap_bps


def _market_snapshot(
    prepared: Any,
    *,
    pack: dict[str, Any],
    book: dict[str, float | None],
    premium_row: dict[str, float] | None,
    ticker: dict[str, Any],
    ws_snap: dict[str, Any] | None = None,
    spot_extra: dict[str, float] | None = None,
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
        "oi_z": _series_z(pack.get("oi_series")),
        "oi_chg_4h_pct": _series_chg_pct(pack.get("oi_series")),
        "gls_z": _series_z(pack.get("gls_series")),
        "gls_chg_4h_pct": _series_chg_pct(pack.get("gls_series")),
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
        **(ws_snap or {}),
        **(spot_extra or {}),
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


def _merge_ws_5m_closed(
    tf: dict[str, Any],
    symbol: str,
    ws_feed: HuntWsFeed | None,
) -> None:
    """Overlay WS grace-closed 5m bar onto REST 5m_closed (lower staleness)."""
    if ws_feed is None:
        return
    overlay = ws_feed.closed_5m_overlay(symbol)
    if not overlay:
        return
    base = tf.get("5m_closed")
    if not isinstance(base, dict) or base.get("status") == "empty":
        tf["5m_closed"] = overlay
        return
    tf["5m_closed"] = {**base, **overlay}


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


def _tf_snapshot_lite(df: Any, *, idx: int = -1) -> dict[str, Any]:
    """OHLC-only snapshot when indicator warmup is insufficient (e.g. new listing 1d)."""
    if df is None or df.is_empty():
        return {"status": "empty"}
    c = _col(df, "close", idx=idx)
    if c <= 0.0:
        return {"status": "empty"}
    lite_rsi = rsi14_from_ohlc(df, idx=idx)
    out: dict[str, Any] = {
        "close": round(c, 6),
        "rsi14": round(lite_rsi, 2) if lite_rsi is not None else None,
        "atr14": None,
        "atr_pct": None,
        "adx14": None,
        "status": "lite",
        "bars": int(df.height),
    }
    if lite_rsi is not None:
        out["rsi14_source"] = "wilder_lite"
    return out


class _LitePrepared:
    """Attribute sink for young listings that cannot pass full prepare_symbol."""

    def __getattr__(self, name: str) -> Any:
        return None


def _lite_prepared(kline_map: dict[str, Any]) -> _LitePrepared:
    p = _LitePrepared()
    for tf_key, attr in (("5m", "work_5m"), ("15m", "work_15m"), ("1h", "work_1h"), ("4h", "work_4h")):
        df = kline_map.get(tf_key)
        work = None
        if df is not None and not df.is_empty():
            work = _prepare_frame(df)
        if work is None or work.is_empty():
            work = df  # raw OHLC fallback: swing/candle helpers only need high/low/close
        setattr(p, attr, work)
    patch_work_4h(p, kline_map)
    return p


def _prev_high(df: Any, *, idx: int) -> float | None:
    """High of the bar BEFORE idx — closed-bar structure break detection."""
    pos = idx if idx >= 0 else df.height + idx
    if pos - 1 < 0:
        return None
    val = _col(df, "high", 0.0, idx=pos - 1)
    return round(val, 6) if val > 0 else None


def _tf_snapshot(df: Any, *, closed: bool = False) -> dict[str, Any]:
    if df is None or df.is_empty():
        return {"status": "empty"}
    idx = -2 if closed and df.height >= 2 else -1
    if "rsi14" not in df.columns:
        out = _tf_snapshot_lite(df, idx=idx)
        if out.get("rsi14") is None:
            out.pop("rsi14", None)  # absent key -> .get(default) works in scorers
        out["candle"] = _candle_shape(df, idx=idx)
        out["closed_bar"] = closed and df.height >= 2
        out["prev_high"] = _prev_high(df, idx=idx)
        return out
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
        "plus_di": round(_col(df, "plus_di14", idx=idx), 2)
        if "plus_di14" in df.columns
        else None,
        "minus_di": round(_col(df, "minus_di14", idx=idx), 2)
        if "minus_di14" in df.columns
        else None,
        "vwap_dev_atr": round(_col(df, "vwap_deviation_atr14", idx=idx), 2)
        if "vwap_deviation_atr14" in df.columns
        else None,
        "bb_width_pctile": round(_col(df, "bb_width_pctile50", idx=idx), 3)
        if "bb_width_pctile50" in df.columns
        else None,
        "obv_rising": bool(_col(df, "obv", idx=idx) > _col(df, "obv_ema20", idx=idx))
        if "obv" in df.columns and "obv_ema20" in df.columns
        else None,
        "squeeze_on": bool(_col(df, "squeeze_on", 0, idx=idx))
        if "squeeze_on" in df.columns
        else None,
        "donchian_width_pct": round(
            (_col(df, "donchian_high20", idx=idx) - _col(df, "donchian_low20", idx=idx)) / c * 100,
            2,
        )
        if c and "donchian_high20" in df.columns and "donchian_low20" in df.columns
        else None,
        "prev_high": _prev_high(df, idx=idx),
        "bearish_rsi_div": bear_div,
        "bullish_rsi_div": bull_div,
        "trend": "bull" if c > e20 > e50 else ("bear" if c < e20 < e50 else "mixed"),
        "candle": _candle_shape(df, idx=idx),
        "closed_bar": closed and df.height >= 2,
    }


# Squeeze-watch (hunt-v3 item 5): volatility compression = pre-pump/pre-dump state.
# TTM squeeze fires BEFORE the move — opposite of every other (post-hoc) trigger here.
SQUEEZE_BB_PCTILE_MAX = 0.20  # bb_width_pctile50 on 1h
SQUEEZE_DONCHIAN_MAX_PCT = 8.0  # 20-bar 1h range, % of price
SQUEEZE_MIN_VOL_24H_M = 5.0  # $5M floor — dead symbols compress forever
SQUEEZE_COOLDOWN_MINUTES = 240


def _squeeze_watch(tf: dict[str, Any], market: dict[str, Any]) -> dict[str, Any] | None:
    """Charged state: 1h BB-width in bottom quintile + narrow Donchian channel."""
    r1h = tf.get("1h") or {}
    pctile = r1h.get("bb_width_pctile")
    don = r1h.get("donchian_width_pct")
    if pctile is None or don is None:
        return None
    charged = float(pctile) <= SQUEEZE_BB_PCTILE_MAX and float(don) <= SQUEEZE_DONCHIAN_MAX_PCT
    if not charged:
        return None
    return {
        "charged": True,
        "bb_width_pctile_1h": float(pctile),
        "donchian_width_pct_1h": float(don),
        "squeeze_on_1h": r1h.get("squeeze_on"),
        "oi_z": market.get("oi_z"),
        "gls_z": market.get("gls_z"),
        "funding_pct": market.get("funding_pct"),
    }


def _format_squeeze_telegram(row: dict[str, Any]) -> str:
    sym = html.escape(str(row["symbol"]).replace("USDT", "-USDT"))
    sq = row.get("squeeze") or {}

    def _v(key: str, fmt: str = "{}") -> str:
        val = sq.get(key)
        return "—" if val is None else fmt.format(val)

    return (
        f"⚡ <b>SQUEEZE CHARGED {sym}</b>\n"
        f"BB-width pctile 1h <code>{_v('bb_width_pctile_1h', '{:.2f}')}</code> · "
        f"Donchian 1h <code>{_v('donchian_width_pct_1h', '{:.1f}%')}</code>\n"
        f"OI z <code>{_v('oi_z')}</code> · global L/S z <code>{_v('gls_z')}</code> · "
        f"fund <code>{_v('funding_pct')}%</code> · vol24h <code>{row.get('vol_24h_m')}M</code>\n"
        f"<i>Компрессия волатильности — заряжен, направление НЕ определено. "
        f"Watch-only, вход только по confirmed-сигналу.</i>"
    )


def _dump_analysis(
    *,
    symbol: str = "",
    price: float,
    tf: dict[str, Any],
    market: dict[str, Any],
    regime: dict[str, Any],
    impulse_high: float,
    impulse_low: float,
    support_break_level: float,
    fib: dict[str, float],
    prev_oi: float | None,
    cur_oi: float | None,
    local_support: float,
    local_resistance: float,
    lifecycle_phase: str = "",
    fall_from_high_pct: float = 0.0,
    pos_in_range: float = 0.5,
    range_pct_24h: float = 0.0,
    leg_gain_pct: float = 0.0,
    pump_stats: dict[str, Any] | None = None,
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

    # Mid-dump continuation (JCT -21% lesson): top-biased triggers go quiet while the
    # dump keeps printing lower closes — credit fresh structural weakness instead.
    if lifecycle_phase == "dump_active":
        if fall_from_high_pct >= 12.0:
            score += 14
            triggers.append("dump_continuation")
        if c5.get("bearish") and c15.get("bearish"):
            score += 10
            triggers.append("bear_momentum_5m_15m")
        if 0 < (r15.get("rsi14") or 0) <= 45:
            score += 8
            triggers.append("rsi15_bear_regime")

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
    # WS smoothed basis premium — overheated perp favors fade short (Q02).
    basis_ap = market.get("basis_ap_bps")
    if basis_ap is not None and float(basis_ap) >= 100.0:
        score += 8
        triggers.append(f"basis_ap_premium_{float(basis_ap):.0f}bps")

    # Series fuel (hunt-v3 item 6): OI flush vs own 4h distribution beats the
    # single 2-point delta; crowded longs (BLESS global L/S 2.06) feed the dump.
    oi_z = market.get("oi_z")
    if oi_z is not None and oi_z <= -1.5:
        score += 8
        triggers.append(f"oi_flush_z{oi_z}")
    gls_z = market.get("gls_z")
    gls = market.get("global_ls_5m") or market.get("global_ls_1h")
    if gls_z is not None and gls_z >= 1.5:
        score += 8
        triggers.append(f"crowded_longs_z{gls_z}")
    elif gls is not None and float(gls) >= 2.0:
        score += 6
        triggers.append("global_ls_extreme_long")

    # Live WS: liquidation cascades + sub-minute taker flow (no REST equivalent).
    liq_score = market.get("liquidation_score_5m")
    if liq_score is not None and float(liq_score) <= -0.25:
        score += 12
        triggers.append(f"ws_liq_cascade_{float(liq_score):.2f}")
    liq_n = market.get("liq_events_5m")
    if liq_n is not None and int(liq_n) >= 8:
        score += 6
        triggers.append(f"ws_liq_storm_{liq_n}")
    ws_agg = market.get("agg_trade_delta_30s")
    if ws_agg is not None and float(ws_agg) < 0.42:
        score += 6
        triggers.append("ws_taker_sell_30s")
    ws_agg60 = market.get("agg_trade_delta_60s")
    if ws_agg60 is not None and float(ws_agg60) <= 0.42:
        score += 8
        triggers.append("ws_taker_sell_60s")
    spot_lead = market.get("spot_lead_return_1m")
    if spot_lead is not None and float(spot_lead) <= -0.4:
        score += 8
        triggers.append(f"spot_lead_dump_{float(spot_lead):.2f}")

    hist_bonus, hist_flags = score_bonus(pump_stats, watch_bias="short")
    if hist_bonus:
        score += hist_bonus
        triggers.extend(hist_flags)

    flt_delta, flt_triggers, flt_blocks = directional_filters(
        tf,
        direction="short",
        pos_in_range=pos_in_range,
        symbol=symbol,
        lifecycle_phase=lifecycle_phase,
        fall_from_high_pct=fall_from_high_pct,
    )
    score = max(0.0, score + flt_delta)
    triggers.extend(flt_triggers)

    # Strict ATR: missing 15m ATR vetoes the setup inside levels (no synthetic fallback).
    atr15 = float(r15.get("atr14") or 0)
    levels = structural_short_levels(
        price=price,
        impulse_high=impulse_high,
        impulse_low=impulse_low,
        fib=fib,
        atr15=atr15,
        local_support=local_support,
        local_resistance=local_resistance,
        range_pct_24h=range_pct_24h,
        leg_gain_pct=leg_gain_pct,
        fall_from_high_pct=fall_from_high_pct,
        symbol=symbol,
        lifecycle_phase=lifecycle_phase,
    )

    return {
        "dump_score": round(score, 1),
        "triggers": triggers,
        "filter_blocks": flt_blocks,
        "levels_viable": levels.get("viable", True),
        "levels_veto": levels.get("veto") or [],
        "support_break_level": support_trigger,
        "resistance_liq": fib.get("ext_1272"),
        "entry_zone": levels["entry_zone"],
        "stop_loss": levels["stop_loss"],
        "tp1": levels["tp1"],
        "tp2": levels["tp2"],
        "tp1_label": levels.get("tp1_label", ""),
        "tp2_label": levels.get("tp2_label", ""),
        "risk_reward": levels.get("risk_reward"),
        "sl_dist_pct": levels.get("sl_dist_pct"),
        "tp2_dist_pct": levels.get("tp2_dist_pct"),
        "invalidation_above": levels["invalidation_above"],
    }


def _confirm_dump(
    dump: dict[str, Any],
    tf: dict[str, Any],
    *,
    symbol: str = "",
    price: float = 0.0,
    market: dict[str, Any] | None = None,
    lifecycle_bias: str = "",
) -> tuple[bool, list[str]]:
    return _se_confirm_dump(
        dump,
        tf,
        symbol=symbol,
        price=price,
        market=market,
        cal=effective_hunt_params(symbol),
        lifecycle_bias=lifecycle_bias,
    )


def _phase(dump: dict[str, Any], confirmed: bool, *, symbol: str = "", lifecycle_note: str | None = None) -> str:
    return _se_phase_dump(
        dump,
        confirmed,
        lifecycle_note=lifecycle_note,
        cal=effective_hunt_params(symbol),
    )


def _long_analysis(
    *,
    symbol: str = "",
    price: float,
    tf: dict[str, Any],
    market: dict[str, Any],
    regime: dict[str, Any],
    impulse_low: float,
    impulse_high: float,
    fib: dict[str, float],
    prev_oi: float | None,
    cur_oi: float | None,
    lifecycle_phase: str | None = None,
    fall_from_high_pct: float = 0.0,
    pos_in_range: float = 0.5,
    range_pct_24h: float = 0.0,
    leg_gain_pct: float = 0.0,
    pump_stats: dict[str, Any] | None = None,
    chg_24h_pct: float = 0.0,
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
    r5_closed = float((tf.get("5m_closed") or {}).get("close") or 0)
    if r5_closed > resistance_break:
        score += 28
        triggers.append(f"broke_resistance_{resistance_break}")
    elif price > resistance_break and r5_closed > 0:
        score += 8
        triggers.append("live_above_resistance_unconfirmed")

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
    # Smoothed basis (ap − index) — report Q02; prefer over raw mark−index for scoring.
    basis_ap = market.get("basis_ap_bps")
    if basis_ap is not None and float(basis_ap) <= -80.0:
        score += 6
        triggers.append(f"basis_ap_discount_{float(basis_ap):.0f}bps")
    drop_from_high = (impulse_high - price) / impulse_high if impulse_high else 0.0
    still_below_structure = impulse_high > 0 and price < impulse_high * 0.92
    if drop_from_high >= 0.08 and r15.get("rsi14", 50) <= 38:
        if still_below_structure:
            score += 6
            triggers.append("post_dump_oversold_watch_only")
        else:
            score += 18
            triggers.append("post_dump_oversold_bounce")
    if drop_from_high >= 0.12 and c5.get("bullish") and c5.get("lower_wick_ratio", 0) >= 0.3:
        if still_below_structure:
            score += 4
            triggers.append("capitulation_wick_watch")
        else:
            score += 14
            triggers.append("capitulation_wick")
    if lifecycle_phase == "dump_active" or (
        still_below_structure and max(drop_from_high * 100.0, fall_from_high_pct) >= 12.0
    ):
        cap = 42.0 if lifecycle_phase == "dump_active" else 48.0
        if score > cap:
            score = cap
            triggers.append("mid_dump_long_cap")

    # Series fuel (item 6): OI build with crowd short = short-squeeze powder.
    oi_z = market.get("oi_z")
    if oi_z is not None and oi_z >= 1.5:
        score += 8
        triggers.append(f"oi_build_z{oi_z}")
    gls_z = market.get("gls_z")
    gls = market.get("global_ls_5m") or market.get("global_ls_1h")
    if gls_z is not None and gls_z <= -1.5:
        score += 8
        triggers.append(f"crowded_shorts_z{gls_z}")
    elif gls is not None and 0 < float(gls) <= 0.5:
        score += 6
        triggers.append("global_ls_extreme_short")

    liq_score = market.get("liquidation_score_5m")
    if liq_score is not None and float(liq_score) >= 0.25:
        score += 12
        triggers.append(f"ws_liq_squeeze_{float(liq_score):.2f}")
    liq_n = market.get("liq_events_5m")
    if liq_n is not None and int(liq_n) >= 8:
        score += 6
        triggers.append(f"ws_liq_storm_{liq_n}")
    ws_agg = market.get("agg_trade_delta_30s")
    if ws_agg is not None and float(ws_agg) > 0.58:
        score += 6
        triggers.append("ws_taker_buy_30s")
    ws_agg60 = market.get("agg_trade_delta_60s")
    if ws_agg60 is not None and float(ws_agg60) >= 0.58:
        score += 8
        triggers.append("ws_taker_buy_60s")
    spot_lead = market.get("spot_lead_return_1m")
    if spot_lead is not None and float(spot_lead) >= 0.4:
        score += 8
        triggers.append(f"spot_lead_pump_{float(spot_lead):.2f}")

    if lifecycle_phase in ("impulse_initiating", "breakout_arming"):
        score += 14
        triggers.append(f"initial_pump_{lifecycle_phase}")
    if lifecycle_phase == "impulse_initiating" and leg_gain_pct >= 25.0 and pos_in_range >= 0.45:
        score += 10
        triggers.append(f"leg_gain_impulse_{leg_gain_pct:.0f}")

    hist_bonus, hist_flags = score_bonus(pump_stats, watch_bias="long")
    if hist_bonus:
        score += hist_bonus
        triggers.extend(hist_flags)

    flt_delta, flt_triggers, flt_blocks = directional_filters(
        tf,
        direction="long",
        pos_in_range=pos_in_range,
        symbol=symbol,
        lifecycle_phase=lifecycle_phase or "",
        fall_from_high_pct=fall_from_high_pct,
        chg_24h_pct=chg_24h_pct,
    )
    score = max(0.0, score + flt_delta)
    triggers.extend(flt_triggers)

    atr15 = float(r15.get("atr14") or 0)
    levels = structural_long_levels(
        price=price,
        impulse_high=impulse_high,
        impulse_low=impulse_low,
        fib=fib,
        atr15=atr15,
        local_support=support_zone or impulse_low,
        local_resistance=resistance_break,
        range_pct_24h=range_pct_24h,
        leg_gain_pct=leg_gain_pct,
        fall_from_high_pct=fall_from_high_pct,
        symbol=symbol,
        lifecycle_phase=lifecycle_phase,
    )

    return {
        "long_score": round(score, 1),
        "triggers": triggers,
        "filter_blocks": flt_blocks,
        "levels_viable": levels.get("viable", True),
        "levels_veto": levels.get("veto") or [],
        "resistance_break_level": resistance_break,
        "support_zone": support_zone,
        "entry_zone": levels["entry_zone"],
        "stop_loss": levels["stop_loss"],
        "tp1": levels["tp1"],
        "tp2": levels["tp2"],
        "tp1_label": levels.get("tp1_label", ""),
        "tp2_label": levels.get("tp2_label", ""),
        "risk_reward": levels.get("risk_reward"),
        "sl_dist_pct": levels.get("sl_dist_pct"),
        "tp2_dist_pct": levels.get("tp2_dist_pct"),
        "invalidation_below": levels["invalidation_below"],
        "context_chg_24h_pct": round(float(chg_24h_pct), 2),
        "context_pos_in_range": round(float(pos_in_range), 3),
        "lifecycle_phase": lifecycle_phase,
    }


def _confirm_long(
    long_setup: dict[str, Any],
    tf: dict[str, Any],
    *,
    symbol: str = "",
    price: float = 0.0,
    market: dict[str, Any] | None = None,
    lifecycle_bias: str = "",
    lifecycle_phase: str = "",
) -> tuple[bool, list[str]]:
    return _se_confirm_long(
        long_setup,
        tf,
        symbol=symbol,
        price=price,
        market=market,
        cal=effective_hunt_params(symbol),
        lifecycle_bias=lifecycle_bias,
        lifecycle_phase=lifecycle_phase,
    )


def _setup_formed(setup: dict[str, Any], *, direction: str, symbol: str = "") -> bool:
    fuel_key = "dump_fuel" if direction == "short" else "long_fuel"
    score_key = "dump_score" if direction == "short" else "long_score"
    fuel = float(setup.get(fuel_key) or setup.get(score_key) or 0)
    return fuel >= effective_hunt_params(symbol).forming_min_score


def _min_rr_for_alert(
    *,
    symbol: str,
    direction: str,
    lifecycle: dict[str, Any] | None,
) -> float:
    sym = symbol.upper()
    cal = effective_hunt_params(sym)
    if sym in PINNED_SYMBOLS:
        return cal.pinned_min_risk_reward
    phase = str((lifecycle or {}).get("phase") or "")
    if direction == "long" and phase == "post_dump_bounce":
        return BOUNCE_MIN_RISK_REWARD
    return cal.min_risk_reward


def _should_alert(
    setup: dict[str, Any],
    *,
    direction: str,
    symbol: str,
    lifecycle: Any | None = None,
    row: dict[str, Any] | None = None,
) -> bool:
    return evaluate_alert_gate(
        setup,
        direction=direction,
        symbol=symbol,
        lifecycle=lifecycle,
        row=row,
    ).ok


def _alert_block_reason(
    setup: dict[str, Any],
    *,
    direction: str,
    symbol: str,
    lifecycle: Any | None = None,
    row: dict[str, Any] | None = None,
) -> str:
    return evaluate_alert_gate(
        setup,
        direction=direction,
        symbol=symbol,
        lifecycle=lifecycle,
        row=row,
    ).message


def _phase_long(long_setup: dict[str, Any], confirmed: bool, *, symbol: str = "") -> str:
    return _se_phase_long(long_setup, confirmed, cal=effective_hunt_params(symbol))


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


async def _maybe_send_early_alert(
    broadcaster: Any,
    *,
    symbol: str,
    direction: str,
    setup: dict[str, Any],
    row: dict[str, Any],
    lifecycle_raw: Any,
    state: dict[str, str],
    mode: str,
    now: datetime,
) -> bool:
    """Prep/start Telegram before full closed-bar confirm."""
    if not early_telegram_enabled(symbol):
        return False
    early = evaluate_early_alert(
        setup,
        direction=direction,
        symbol=symbol,
        lifecycle=lifecycle_raw,
        row=row,
    )
    if early.kind in ("none", "confirm"):
        return False
    lc_phase = str((lifecycle_raw or {}).get("phase") or "")
    if (
        direction == "short"
        and mode not in ("short", "both")
        and lc_phase
        not in ("dump_active", "exhaustion_at_high", "distribution")
    ):
        return False
    if (
        direction == "long"
        and mode not in ("long", "both")
        and lc_phase
        not in (
            "post_dump_bounce",
            "accumulation",
            "recovery",
            "breakout_arming",
            "impulse_initiating",
        )
    ):
        return False
    if not early_cooldown_ok(symbol, direction, early.tier, state, now=now):
        return False
    msg = format_early_telegram(
        row,
        direction=direction,
        setup=setup,
        lifecycle=lifecycle_raw,
        alert=early,
    )
    result = await broadcaster.send_html(msg)
    if result.status != "sent":
        LOG.warning(
            "watch_early_telegram_failed",
            symbol=symbol,
            direction=direction,
            tier=early.tier,
            status=result.status,
            reason=result.reason,
        )
        return False
    mark_early_sent(symbol, direction, early.tier, state, now=now)
    event_kind = {"prep": "prep", "imminent": "imminent", "start": "start"}.get(
        early.tier, "forming_early"
    )
    LOG.info(
        "watch_early_telegram_sent",
        symbol=symbol,
        direction=direction,
        tier=early.tier,
        message_id=result.message_id,
    )
    append_signal_event(
        event_kind,
        symbol=symbol,
        direction=direction,
        detail=early.message,
        payload={
            "tier": early.tier,
            "message_id": result.message_id,
            "fuel": setup.get("dump_fuel") or setup.get("long_fuel"),
            "phase": setup.get("phase"),
            "lifecycle_phase": lc_phase,
        },
    )
    return True


def _cooldown_ok(
    symbol: str,
    direction: str,
    state: dict[str, str],
    *,
    now: datetime,
    minutes: int = COOLDOWN_MINUTES,
) -> bool:
    key = f"{symbol}:{direction}"
    raw = state.get(key) or state.get(symbol)
    if not raw:
        return True
    try:
        last = datetime.fromisoformat(raw)
    except ValueError:
        return True
    return now - last >= timedelta(minutes=minutes)


def _entry_past_tp1(setup: dict[str, Any], *, direction: str, price: float) -> bool:
    """Reject TG when price already at/through TP1 — instant TP1 + invalidate ping-pong."""
    tp1 = float(setup.get("tp1") or 0)
    if tp1 <= 0 or price <= 0:
        return False
    if direction == "short":
        return price <= tp1
    return price >= tp1


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
    fuel_key = "dump_fuel" if direction == "short" else "long_fuel"
    phase = str(setup.get("phase") or "—")
    confirmed = bool(setup.get("confirmed"))
    badge = _phase_badge(phase, confirmed, direction=direction)

    def _opt_num(val: Any, *, digits: int = 4) -> str:
        if val is None:
            return "—"
        try:
            return f"{float(val):.{digits}f}"
        except (TypeError, ValueError):
            return "—"

    fuel = _opt_num(setup.get(fuel_key)) if setup.get(fuel_key) is not None else "—"
    score = _opt_num(setup.get(score_key)) if setup.get(score_key) is not None else "—"
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
    else:
        level_line = (
            f"Resistance <code>{_fmt_price(setup.get('resistance_break_level'))}</code> · support "
            f"<code>{_fmt_price(setup.get('support_zone'))}</code> · impulse L "
            f"<code>{_fmt_price(row.get('impulse_low'))}</code>"
        )

    lines = [
        f"{badge} <b>{dir_label}</b> · <code>{phase}</code> · "
        f"fuel <code>{fuel}</code> · raw <code>{score}</code>",
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
            f"OI <code>{_fmt_price(oi if oi is not None else None)}</code> · "
            f"Δ5m <code>{_opt_num(oi_chg)}</code> · "
            f"fund <code>{_opt_num(fund, digits=3)}%</code> · "
            f"taker5m <code>{_opt_num(taker)}</code> · "
            f"L/S <code>{_opt_num(ls)}</code>"
        ),
        f"Triggers: <code>{trig_txt or '—'}</code>",
    ]
    if confirmed:
        hard = setup.get("confirm_hard") or []
        lines.append(f"<b>✅ CONFIRM</b> {html.escape(', '.join(str(x) for x in hard))}")
    return lines


_PHASE_HUMAN: dict[str, str] = {
    "dump_active": "Активный дамп",
    "dump_initiating": "Начало дампа",
    "dump_imminent": "Дамп неизбежен",
    "dump_setup_forming": "Формируется шорт",
    "dump_confirmed": "Шорт подтверждён",
    "exhaustion_at_high": "Истощение на хаях",
    "exhaustion_watch": "Наблюдение за истощением",
    "distribution": "Распределение",
    "impulse_initiating": "Начало импульса",
    "breakout_arming": "Вооружение пробоя",
    "post_dump_bounce": "Отскок после дампа",
    "accumulation": "Накопление",
    "accumulation_watch": "Наблюдение за накоплением",
    "long_imminent": "Лонг неизбежен",
    "long_setup_forming": "Формируется лонг",
    "long_confirmed": "Лонг подтверждён",
    "no_setup": "Нет сетапа",
    "no_dump_yet": "Нет дампа",
    "no_long_yet": "Нет лонга",
}


def _phase_human(phase: str) -> str:
    return _PHASE_HUMAN.get(phase, phase)


def _pct_str(a: float, b: float, direction: str) -> str:
    """Percentage distance from entry to target."""
    if a <= 0 or b <= 0:
        return ""
    if direction == "short":
        pct = (a - b) / a * 100.0
    else:
        pct = (b - a) / a * 100.0
    return f"+{pct:.1f}%"


def _reason_human(setup: dict[str, Any], *, direction: str, lc_phase: str) -> str:
    """Build human-readable reason line from phase + triggers + fuel."""
    phase_txt = _phase_human(lc_phase) if lc_phase and lc_phase != "—" else _phase_human(
        str(setup.get("phase") or "")
    )
    triggers = setup.get("triggers") or []
    trig_short: list[str] = []
    for t in triggers[:3]:
        ts = str(t)
        if "volume" in ts or "vol" in ts:
            trig_short.append("аномальный объём")
        elif "support" in ts or "break" in ts:
            trig_short.append("пробой поддержки")
        elif "resistance" in ts:
            trig_short.append("пробой сопротивления")
        elif "cascade" in ts or "liq" in ts:
            trig_short.append("каскад ликвидаций")
        elif "rejection" in ts:
            trig_short.append("отбой от уровня")
        elif "rsi" in ts or "div" in ts:
            trig_short.append("RSI-дивергенция")
        elif "funding" in ts:
            trig_short.append("перегрев фандинга")
        elif "oi" in ts:
            trig_short.append("аномалия OI")
        elif "whale" in ts:
            trig_short.append("крупный продавец")
        else:
            trig_short.append(ts.replace("_", " ").split(":")[0])
    trig_txt = ", ".join(dict.fromkeys(trig_short))  # deduplicate, keep order
    if phase_txt and trig_txt:
        return f"{phase_txt} · {trig_txt}"
    return phase_txt or trig_txt or "—"


def _format_telegram(row: dict[str, Any], *, direction: str, confirm_reasons: list[str]) -> str:
    sym = html.escape(str(row["symbol"]).replace("USDT", "-USDT"))
    setup = row["dump"] if direction == "short" else row["long"]
    fuel_key = "dump_fuel" if direction == "short" else "long_fuel"
    score_key = "dump_score" if direction == "short" else "long_score"
    price = float(row.get("price") or 0)
    tf = row.get("timeframes") or {}
    pos = row.get("market") or row.get("positioning") or {}
    lc = row.get("lifecycle") or {}
    lc_phase = str(lc.get("phase") or "—")

    badge = "🔴" if direction == "short" else "🟢"
    dir_label = "SHORT" if direction == "short" else "LONG"

    fuel_val = setup.get(fuel_key)
    score_val = setup.get(score_key)
    fuel = float(fuel_val) if fuel_val is not None else 0.0
    fuel_str = f"{fuel:.0f}" if fuel_val is not None else "—"
    score_str = f"{float(score_val):.0f}" if score_val is not None else "—"

    # Signal quality rating
    _strong_phases = frozenset({"dump_active","exhaustion_at_high","distribution","dump_confirmed",
                                 "accumulation","impulse_initiating","breakout_arming","long_confirmed"})
    if fuel >= 80 and lc_phase in _strong_phases:
        rating = "🔥 СИЛЬНЫЙ"
    elif fuel >= 65 and lc_phase in _strong_phases:
        rating = "✅ УВЕРЕННЫЙ"
    elif fuel >= 50:
        rating = "⚠️ СРЕДНИЙ"
    else:
        rating = "📊 СЛАБЫЙ"

    lifecycle_line = html.escape(_phase_human(lc_phase)) if lc_phase != "—" else "—"

    ez = setup.get("entry_zone") or [price, price]
    entry_lo = _fmt_price(ez[0])
    entry_hi = _fmt_price(ez[1])
    sl = _fmt_price(setup.get("stop_loss"))
    tp1 = setup.get("tp1")
    tp2 = setup.get("tp2")
    tp1_pct = _pct_str(price, float(tp1), direction) if tp1 else ""
    tp2_pct = _pct_str(price, float(tp2), direction) if tp2 else ""
    tp1_lbl = setup.get("tp1_label") or ""
    tp2_lbl = setup.get("tp2_label") or ""
    tp1_str = f"<code>{_fmt_price(tp1)}</code>" + (f" (<b>{tp1_pct}</b>)" if tp1_pct else "") + (f" · {tp1_lbl}" if tp1_lbl else "")
    tp2_str = f"<code>{_fmt_price(tp2)}</code>" + (f" (<b>{tp2_pct}</b>)" if tp2_pct else "") + (f" · {tp2_lbl}" if tp2_lbl else "")

    reason = _reason_human(setup, direction=direction, lc_phase=lc_phase)

    header = f"{badge} <b>ВХОД ВЗЯТ · {sym} {dir_label}</b>  {rating}"
    phase_line = f"📌 {lifecycle_line}"
    entry_line = f"📍 Вход: <code>{entry_lo}–{entry_hi}</code>  |  Стоп: <code>{sl}</code>"
    tp_line = f"🎯 TP1: {tp1_str}  |  TP2: {tp2_str}"
    reason_line = f"💡 {html.escape(reason)}"
    score_line = f"📊 Score: <code>{score_str}</code> · Fuel: <code>{fuel_str}</code>"
    footer = "<i>Signal-only · closed 5m/1m confirm · открывай сделку вручную</i>"

    hist = format_history_telegram(row.get("pump_history"))
    hist_line = f"{html.escape(hist)}\n" if hist else ""

    return f"{header}\n{phase_line}\n{entry_line}\n{tp_line}\n{reason_line}\n{score_line}\n{hist_line}\n{footer}"


# Orphan signals (symbol no longer in watchlist) are re-checked via REST klines.
ORPHAN_RECONCILE_MINUTES = 5
INWATCH_KLINE_RECONCILE_SECONDS = 45


async def _reconcile_inwatch_active(
    client: BinanceFuturesMarketData,
    tracker_state: dict[str, Any],
    *,
    symbol: str,
    now: datetime,
) -> list[Any]:
    """5m kline hi/lo since last_checked_at for active signals still in the watchlist."""
    events: list[Any] = []
    signals = tracker_state.get("signals") or {}
    sym_u = symbol.upper()
    for key, sig in list(signals.items()):
        if not isinstance(sig, dict) or sig.get("status") != "active":
            continue
        o_sym, _, o_dir = key.partition(":")
        if o_sym != sym_u:
            continue
        anchor_raw = sig.get("last_checked_at") or sig.get("opened_at")
        try:
            anchor = datetime.fromisoformat(str(anchor_raw))
        except (TypeError, ValueError):
            anchor = now
        if (now - anchor).total_seconds() < INWATCH_KLINE_RECONCILE_SECONDS:
            continue
        df = await _safe_fetch(
            client.fetch_klines_between(
                o_sym,
                "5m",
                start_time_ms=int(anchor.timestamp() * 1000),
                end_time_ms=int(now.timestamp() * 1000),
            )
        )
        if df is None or df.is_empty():
            sig["last_checked_at"] = now.isoformat()
            continue
        hi = float(df["high"].max())
        lo = float(df["low"].min())
        last_price = float(df["close"][-1])
        events.extend(
            reconcile_signal(
                tracker_state,
                symbol=o_sym,
                direction=o_dir,
                hi=hi,
                lo=lo,
                last_price=last_price,
                ts=now,
            )
        )
    return events


async def _reconcile_orphan_signals(
    client: BinanceFuturesMarketData,
    tracker_state: dict[str, Any],
    *,
    seen_symbols: set[str],
    now: datetime,
) -> list[Any]:
    events: list[Any] = []
    signals = tracker_state.get("signals") or {}
    for key, sig in list(signals.items()):
        if not isinstance(sig, dict) or sig.get("status") != "active":
            continue
        o_sym, _, o_dir = key.partition(":")
        if not o_sym or not o_dir or o_sym in seen_symbols:
            continue
        anchor_raw = sig.get("last_checked_at") or sig.get("opened_at")
        try:
            anchor = datetime.fromisoformat(str(anchor_raw))
        except (TypeError, ValueError):
            anchor = now
        if (now - anchor).total_seconds() < ORPHAN_RECONCILE_MINUTES * 60:
            continue
        df = await _safe_fetch(
            client.fetch_klines_between(
                o_sym,
                "5m",
                start_time_ms=int(anchor.timestamp() * 1000),
                end_time_ms=int(now.timestamp() * 1000),
            )
        )
        if df is None or df.is_empty():
            sig["last_checked_at"] = now.isoformat()
            continue
        hi = float(df["high"].max())
        lo = float(df["low"].min())
        last_price = float(df["close"][-1])
        events.extend(
            reconcile_signal(
                tracker_state,
                symbol=o_sym,
                direction=o_dir,
                hi=hi,
                lo=lo,
                last_price=last_price,
                ts=now,
            )
        )
    return events


def _duration_str(opened: str) -> str:
    """Human-readable duration from ISO opened_at to now."""
    try:
        from datetime import UTC, datetime
        start = datetime.fromisoformat(opened.replace(" ", "T"))
        if start.tzinfo is None:
            start = start.replace(tzinfo=UTC)
        delta = datetime.now(UTC) - start
        total_m = int(delta.total_seconds() // 60)
        h, m = divmod(total_m, 60)
        if h > 0:
            return f"{h}ч {m}м"
        return f"{m}м"
    except Exception:
        return "—"


def _format_followup_telegram(followup: Any, row: dict[str, Any]) -> str:
    sym = html.escape(str(followup.symbol).replace("USDT", "-USDT"))
    direction = followup.direction.upper()
    price = _fmt_price(followup.price)
    lc = row.get("lifecycle") or {}
    payload = followup.payload if isinstance(followup.payload, dict) else {}
    event = followup.event

    # Use levels frozen at entry (payload), not live recalculated setup on this tick.
    sl = _fmt_price(payload.get("stop_loss"))
    tp1_lvl = _fmt_price(payload.get("tp1"))
    tp2_lvl = _fmt_price(payload.get("tp2"))
    entry_lo = payload.get("entry_lo")
    entry_hi = payload.get("entry_hi")
    entry_zone = (
        f"{_fmt_price(entry_lo)}–{_fmt_price(entry_hi)}"
        if entry_lo is not None and entry_hi is not None
        else "—"
    )
    opened_raw = str(payload.get("opened_at") or "")[:19].replace("T", " ")
    msg_id = payload.get("entry_message_id")
    entry_ref = f"Вход {entry_zone}"
    if msg_id:
        entry_ref += f" · сигнал TG <code>#{msg_id}</code>"

    reason_raw = str(payload.get("reason") or "")
    detail_human = invalidate_detail_human(str(followup.detail or ""), reason=reason_raw)

    # TP1 hit: structured update card
    if event == "fix_profit_tp1":
        fix_pct = int(payload.get("partial_fixed_pct") or 50)
        new_sl = _fmt_price(payload.get("stop_loss"))
        tp1_pct_val = payload.get("tp1")
        entry_price_est = entry_lo or 0
        if tp1_pct_val and entry_price_est:
            try:
                if direction == "SHORT":
                    tp1_pct = (float(entry_price_est) - float(tp1_pct_val)) / float(entry_price_est) * 100.0
                else:
                    tp1_pct = (float(tp1_pct_val) - float(entry_price_est)) / float(entry_price_est) * 100.0
                tp1_pct_str = f" +{tp1_pct:.1f}%"
            except Exception:
                tp1_pct_str = ""
        else:
            tp1_pct_str = ""
        return (
            f"✅ <b>TP1 достигнут{tp1_pct_str} · {sym} {direction}</b>\n"
            f"🔒 Зафиксируй <b>{fix_pct}%</b> позиции · Стоп перенесён на безубыток <code>{new_sl}</code>\n"
            f"🎯 Следующая цель: TP2 <code>{tp2_lvl}</code>\n"
            f"{entry_ref}\n"
            f"<i>Hunt follow-up · не auto-trade</i>"
        )

    # TP2 hit: close card
    if event == "fix_profit_tp2":
        duration = _duration_str(opened_raw)
        skipped = bool(payload.get("tp1_skipped"))
        extra = " (TP1 пролёт)" if skipped else ""
        return (
            f"📋 <b>Закрыт {sym} {direction}{extra}</b>\n"
            f"💰 PnL: TP2 <code>{tp2_lvl}</code> · Длит: {duration}\n"
            f"📌 Причина: Достигнут TP2\n"
            f"{entry_ref}\n"
            f"<i>Hunt follow-up · не auto-trade</i>"
        )

    # Signal closed / invalidated
    if event == "invalidate":
        duration = _duration_str(opened_raw)
        reason_label_map = {
            "stop_hit": "Стоп-лосс пробит",
            "tp1": "Закрыто по TP1",
            "tp2": "Закрыто по TP2",
            "bounce_invalidate": "Lifecycle: отскок — шорт отменён",
            "time_stall": "Нет прогресса за 8ч — тезис не сработал",
            "bias_flip": "Lifecycle сменил bias против позиции",
            "support_lost": "Потеря поддержки (лонг)",
        }
        reason_str = reason_label_map.get(reason_raw, html.escape(detail_human))
        return (
            f"📋 <b>Закрыт {sym} {direction}</b>\n"
            f"📌 Причина: {reason_str}\n"
            f"⏱ Длит: {duration}\n"
            f"{entry_ref}\n"
            f"Уровни: SL <code>{sl}</code> · TP1 <code>{tp1_lvl}</code> · TP2 <code>{tp2_lvl}</code>\n"
            f"<i>Hunt follow-up · не auto-trade</i>"
        )

    # Stop warning
    if event == "stop_warning":
        return (
            f"⚠️ <b>СТОП РЯДОМ · {sym} {direction}</b>\n"
            f"Цена <code>{price}</code> близко к SL <code>{sl}</code>\n"
            f"Реши: держать или фиксировать вручную.\n"
            f"{entry_ref}\n"
            f"<i>Hunt follow-up · не auto-trade</i>"
        )

    # Generic fallback
    badges = {"phase_change": "🔄", "avg_zone": "➕"}
    titles = {"phase_change": "PHASE CHANGE", "avg_zone": "AVG ZONE"}
    badge = badges.get(event, "📣")
    title = titles.get(event, event)
    lc_phase_now = html.escape(_phase_human(str(lc.get("phase") or "—")))
    return (
        f"{badge} <b>{title}</b>\n"
        f"{sym} · <code>{direction}</code> · цена <code>{price}</code>\n"
        f"{html.escape(detail_human)}\n"
        f"{entry_ref}\n"
        f"SL <code>{sl}</code> · TP1 <code>{tp1_lvl}</code> · TP2 <code>{tp2_lvl}</code>\n"
        f"Фаза: {lc_phase_now}\n"
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
    premium_all: dict[str, dict[str, float]],
    funding_info_all: dict[str, dict[str, float | int]],
    btc_work_1h: Any | None,
    exchange_by_sym: dict[str, Any],
    ticker_by_sym: dict[str, dict[str, Any]],
    ws_feed: HuntWsFeed | None = None,
    spot_companion: SpotCompanionService | None = None,
    stagger_klines_ms: int = 0,
    pump_stats: dict[str, Any] | None = None,
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
        strategy_fits=(),
    )
    limits = _kline_limits(minimums)
    tf_order = ("1m", "3m", "5m", "15m", "1h", "4h", "1d")
    kline_map: dict[str, Any] = {}
    if stagger_klines_ms > 0:
        for name in tf_order:
            res = await _safe_fetch(
                client.fetch_klines_cached(symbol, name, limit=limits[name])
            )
            kline_map[name] = res
            await asyncio.sleep(stagger_klines_ms / 1000.0)
    else:
        kline_tasks = {
            name: client.fetch_klines_cached(symbol, name, limit=limits[name])
            for name in tf_order
        }
        kline_results = await asyncio.gather(*kline_tasks.values(), return_exceptions=True)
        for name, res in zip(kline_tasks.keys(), kline_results, strict=True):
            kline_map[name] = None if isinstance(res, BaseException) else res
    df_1m = kline_map["1m"]
    if df_1m is None or df_1m.is_empty():
        return {"ts": datetime.now(UTC).isoformat(), "symbol": symbol, "error": "klines_1m_failed"}
    df_5m = kline_map["5m"]
    pack = await _fetch_rest_pack(client, symbol)
    liq_skip = liquidity_skip_reason(
        quote_volume=market_row["quote_volume"],
        oi=float(pack.get("oi") or 0) if pack.get("oi") is not None else None,
        last_price=price,
        symbol=symbol,
    )
    if liq_skip:
        return {
            "ts": datetime.now(UTC).isoformat(),
            "symbol": symbol,
            "error": liq_skip,
            "liquidity_skip": True,
        }
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
    young_listing = False
    bars_4h = int(kline_map["4h"].height if kline_map.get("4h") is not None else 0)
    bars_1h = int(kline_map["1h"].height if kline_map.get("1h") is not None else 0)
    if prepared is None:
        young_listing = True
        if should_use_young_lite_path(bars_4h=bars_4h, bars_1h=bars_1h):
            # SOXL/SKHYNIX: native 4h prepare empty despite 50–160 raw bars — synth from 1h.
            prepared = _lite_prepared(kline_map)
        else:
            relaxed = {"5m": 144, "15m": 96, "1h": 24, "4h": 6}
            prepared = prepare_symbol(item, frames, minimums=relaxed, settings=settings)
            if prepared is None:
                prepared = _lite_prepared(kline_map)
            else:
                patch_work_4h(prepared, kline_map)
    else:
        patch_work_4h(prepared, kline_map)

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
    rest_h, rest_l = impulse["hunt_high"], impulse["hunt_low"]
    fib_4h = fib_retracement_levels(ih4, il4)
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
    _merge_ws_5m_closed(tf, symbol, ws_feed)
    ws_snap = ws_feed.snapshot(symbol) if ws_feed is not None else None
    _overlay_ws_market(prepared, ws_snap)
    spot_extra = (
        spot_companion.enrichments_for(symbol) if spot_companion is not None else None
    )
    market = _market_snapshot(
        prepared,
        pack=pack,
        book=book,
        premium_row=premium_row,
        ticker=ticker,
        ws_snap=ws_snap,
        spot_extra=spot_extra,
    )
    regime = _regime_snapshot(prepared)
    hunt_h, hunt_l, session_mem = merge_hunt_extremes(
        symbol,
        price=price,
        rest_hunt_high=rest_h,
        rest_hunt_low=rest_l,
        lifecycle_phase="",
        market=market,
    )
    fib_hunt = fib_retracement_levels(hunt_h, hunt_l)
    fib = {**fib_4h, "hunt": fib_hunt}
    result: dict[str, Any] = {
        "ts": datetime.now(UTC).isoformat(),
        "symbol": symbol,
        "watch_mode": watch_mode,
        "young_listing": young_listing,
        "price": price,
        "chg_24h_pct": round(float(ticker.get("price_change_percent") or 0), 2),
        "vol_24h_m": market.get("vol_24h_m"),
        # NOTE: "positioning" was a byte-identical alias of "market" — it
        # doubled every JSONL row (~45% of file size). Readers fall back
        # market -> positioning for old rows.
        "market": market,
        "regime": regime,
        "timeframes": tf,
        "session": session,
        "squeeze": _squeeze_watch(tf, market),
        "impulse": impulse,
        "impulse_high": hunt_h,
        "impulse_low": hunt_l,
        "session_memory": session_mem,
        "fib": fib,
        "kline_limits": limits,
        "data_quality": _data_quality_report(
            prepared,
            frames=frames,
            df_1m=df_1m,
            pack=pack,
            book=book,
            tf=tf,
        ),
        "book_walls": book_walls_from_depth(pack.get("book_depth")),
    }

    lifecycle = stabilize_lifecycle(
        symbol,
        assess_hunt_lifecycle(
            price=price,
            hunt_high=hunt_h,
            hunt_low=hunt_l,
            session=session,
            tf=tf,
            market=market,
            symbol=symbol,
        ),
    )
    leg_gain_pct = (
        round((hunt_h - hunt_l) / hunt_l * 100.0, 1) if hunt_l > 0 else 0.0
    )
    lifecycle_dict = {
        "phase": lifecycle.phase.value,
        "recommended_bias": lifecycle.recommended_bias,
        "short_entry_ok": lifecycle.short_entry_ok,
        "short_confirm_ok": lifecycle.short_confirm_ok,
        "invalidate_short": lifecycle.invalidate_short,
        "fall_from_high_pct": lifecycle.fall_from_high_pct,
        "bounce_from_low_pct": lifecycle.bounce_from_low_pct,
        "leg_gain_pct": leg_gain_pct,
        "local_support": lifecycle.local_support,
        "local_resistance": lifecycle.local_resistance,
        "reasons": list(lifecycle.reasons),
    }
    result["lifecycle"] = lifecycle_dict
    merge_hunt_extremes(
        symbol,
        price=price,
        rest_hunt_high=rest_h,
        rest_hunt_low=rest_l,
        lifecycle_phase=lifecycle.phase.value,
        market=market,
    )

    # Both sides are always analyzed; watch_mode gates Telegram only (VELVET dump_active
    # was invisible because pinned mode=long skipped _dump_analysis entirely).
    pos_in_range = float(session.get("pos_in_range") or 0.5)
    support_level = effective_support_break(
        impulse_high=hunt_h,
        lifecycle=lifecycle,
        pos_in_range=pos_in_range,
    )
    range_pct_24h = float(session.get("range_pct_24h") or 0)
    dump = _dump_analysis(
        symbol=symbol,
        price=price,
        tf=tf,
        market=market,
        regime=regime,
        impulse_high=hunt_h,
        impulse_low=hunt_l,
        support_break_level=support_level,
        fib=fib_hunt,
        prev_oi=prev_oi,
        cur_oi=prepared.oi_current,
        local_support=lifecycle.local_support,
        local_resistance=lifecycle.local_resistance,
        lifecycle_phase=lifecycle.phase.value,
        fall_from_high_pct=lifecycle.fall_from_high_pct,
        pos_in_range=pos_in_range,
        range_pct_24h=range_pct_24h,
        leg_gain_pct=leg_gain_pct,
        pump_stats=pump_stats,
    )
    dump = enrich_dump_setup(dump)
    dump["lifecycle_phase"] = lifecycle.phase.value
    dump["fall_from_high_pct"] = lifecycle.fall_from_high_pct
    dump["young_listing"] = young_listing
    dump["bars_1h"] = bars_1h
    confirmed, confirm_hard = _confirm_dump(
        dump,
        tf,
        symbol=symbol,
        price=price,
        market=market,
        lifecycle_bias=str(lifecycle.recommended_bias or ""),
    )
    confirmed, confirm_hard, lifecycle_note = apply_short_invalidation(
        confirmed,
        confirm_hard,
        lifecycle,
        dump=dump,
    )
    dump["confirm_hard"] = confirm_hard
    dump["phase"] = _phase(dump, confirmed, symbol=symbol, lifecycle_note=lifecycle_note)
    dump["confirmed"] = confirmed
    dump["lifecycle"] = lifecycle_dict
    if lifecycle_note:
        dump["lifecycle_note"] = lifecycle_note
    if lifecycle.invalidate_short:
        fuel = float(dump.get("dump_fuel") or 0)
        cap = 32.0
        if fuel > cap:
            # Keep fuel/score/confirmed consistent: a capped setup must not
            # stay flagged confirmed (downstream would re-trust the old state).
            dump["dump_fuel"] = cap
            dump["dump_score"] = min(float(dump.get("dump_score") or 0), cap)
            dump["confirmed"] = False
            dump["triggers"] = [*list(dump.get("triggers") or []), "lifecycle_short_cap"]
            dump["phase"] = _phase(dump, confirmed=False, symbol=symbol, lifecycle_note="lifecycle_invalidate_short")
    result["dump"] = dump

    chg24 = float(result.get("chg_24h_pct") or 0)
    long_setup = _long_analysis(
        symbol=symbol,
        price=price,
        tf=tf,
        market=market,
        regime=regime,
        impulse_low=hunt_l,
        impulse_high=hunt_h,
        fib=fib_hunt,
        prev_oi=prev_oi,
        cur_oi=prepared.oi_current,
        lifecycle_phase=lifecycle.phase.value,
        fall_from_high_pct=lifecycle.fall_from_high_pct,
        pos_in_range=pos_in_range,
        range_pct_24h=range_pct_24h,
        leg_gain_pct=leg_gain_pct,
        pump_stats=pump_stats,
        chg_24h_pct=chg24,
    )
    long_setup = enrich_long_setup(long_setup)
    long_setup["young_listing"] = young_listing
    long_setup["bars_1h"] = bars_1h
    long_confirmed, long_hard = _confirm_long(
        long_setup,
        tf,
        symbol=symbol,
        price=price,
        market=market,
        lifecycle_bias=str(lifecycle.recommended_bias or ""),
        lifecycle_phase=lifecycle.phase.value,
    )
    long_setup["confirm_hard"] = long_hard
    long_setup["lifecycle_phase"] = lifecycle.phase.value
    long_setup["phase"] = _phase_long(long_setup, long_confirmed, symbol=symbol)
    long_setup["confirmed"] = long_confirmed
    result["long"] = long_setup

    return result


async def _run_tick(
    symbols: tuple[str, ...],
    *,
    settings: Any,
    minimums: dict[str, int],
    client: BinanceFuturesMarketData,
    prev_oi: dict[str, float | None],
    last_bias: dict[str, str],
    mode_map: dict[str, WatchMode],
    broadcaster: TelegramBroadcaster | None,
    send_telegram: bool,
    ticker_by_sym: dict[str, dict[str, Any]] | None = None,
    ignition_by_sym: dict[str, dict[str, Any]] | None = None,
    pump_stats_by_sym: dict[str, dict[str, Any]] | None = None,
    pump_store: Any | None = None,
    ws_feed: HuntWsFeed | None = None,
    spot_companion: SpotCompanionService | None = None,
) -> list[dict[str, Any]]:
    state = _load_state()
    tracker_state = load_tracker_state()
    prep_shadow_state = load_prep_shadow_state()
    now = datetime.now(UTC)
    rows: list[dict[str, Any]] = []
    notify_pending = {str(p.get("symbol")): p for p in load_pending_notify()}
    try:
        premium_all = await _safe_fetch(client.fetch_premium_index_all()) or {}
        funding_info_all = await _safe_fetch(client.fetch_funding_info_all()) or {}
        exchange_list = await _safe_fetch(client.fetch_exchange_symbols()) or []
        exchange_by_sym = {r.symbol: r for r in exchange_list}
        if ticker_by_sym is None:
            ticker_raw = await _safe_fetch(client.fetch_ticker_24h()) or []
            ticker_by_sym = {str(t.get("symbol")): t for t in ticker_raw if t.get("symbol")}
        if spot_companion is not None and symbols:
            futures_mids = {
                s: float((ticker_by_sym.get(s) or {}).get("last_price") or 0) or None
                for s in symbols
            }
            try:
                spot_n = await spot_companion.refresh_symbols(
                    list(symbols), futures_mid_by_symbol=futures_mids
                )
                LOG.debug("spot_companion_refresh", symbols=len(symbols), updated=spot_n)
            except defensive_exc_types(asyncio.IncompleteReadError) as exc:
                LOG.warning("spot_companion_refresh_failed", error=repr(exc))
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
                    lifecycle_bias=last_bias.get(symbol),
                )
                row = await asyncio.wait_for(
                    _snapshot_symbol(
                        client,
                        settings,
                        minimums,
                        symbol,
                        watch_mode=mode,
                        prev_oi=prev_oi.get(symbol),
                        premium_all=premium_all,
                        funding_info_all=funding_info_all,
                        btc_work_1h=btc_work_1h,
                        exchange_by_sym=exchange_by_sym,
                        ticker_by_sym=ticker_by_sym,
                        ws_feed=ws_feed,
                        spot_companion=spot_companion,
                        pump_stats=(
                            pump_stats_by_sym.get(symbol) if pump_stats_by_sym else None
                        ),
                    ),
                    timeout=SYMBOL_TICK_TIMEOUT_S,
                )
                row = latch_row_setups(tracker_state, row)
                oi_val = (row.get("market") or row.get("positioning") or {}).get("oi")
                if oi_val is not None:
                    prev_oi[symbol] = float(oi_val)
                rows.append(row)
                if ignition_by_sym and symbol in ignition_by_sym:
                    row["ignited"] = True
                    row["ignition"] = ignition_by_sym[symbol]
                promote_initial_pump_lifecycle(row, symbol=symbol)
                if pump_stats_by_sym and symbol in pump_stats_by_sym:
                    row["pump_history"] = pump_stats_by_sym[symbol]
                dump = row.get("dump") or {}
                long_setup = row.get("long") or {}
                lifecycle_raw = row.get("lifecycle") or (dump.get("lifecycle") if dump else None)
                if lifecycle_raw and isinstance(lifecycle_raw, dict):
                    last_bias[symbol] = str(lifecycle_raw.get("recommended_bias") or "")
                    mode = effective_watch_mode(
                        symbol,
                        mode_map,
                        lifecycle_bias=last_bias[symbol],
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
                    data_missing=(row.get("data_quality") or {}).get("fields_missing"),
                )
                for prep_dir, prep_setup in (("short", dump), ("long", long_setup)):
                    if prep_setup:
                        process_prep_shadow(
                            prep_shadow_state,
                            symbol=symbol,
                            direction=prep_dir,
                            setup=prep_setup,
                            row=row,
                            lifecycle=lifecycle_raw,
                            now=now,
                        )
                kline_events = await _reconcile_inwatch_active(
                    client, tracker_state, symbol=symbol, now=now
                )
                if kline_events:
                    mark_followups_sent(tracker_state, kline_events, now=now)
                    for fu in kline_events:
                        LOG.info(
                            "watch_followup_kline",
                            symbol=fu.symbol,
                            followup_event=fu.event,
                            detail=fu.detail,
                        )
                followups = evaluate_followups(tracker_state, row, now=now)
                for fu in followups:
                    LOG.info(
                        "watch_followup",
                        symbol=fu.symbol,
                        followup_event=fu.event,
                        detail=fu.detail,
                    )
                    # State machine runs for every signal; messages only for
                    # signals that were actually announced in Telegram.
                    announced = bool((fu.payload or {}).get("announced", True))
                    if send_telegram and broadcaster is not None and announced:
                        msg = _format_followup_telegram(fu, row)
                        result = await broadcaster.send_html(msg)
                        if result.status == "sent":
                            LOG.info(
                                "watch_followup_sent",
                                symbol=fu.symbol,
                                followup_event=fu.event,
                                message_id=result.message_id,
                            )
                squeeze = row.get("squeeze")
                if squeeze and float(row.get("vol_24h_m") or 0) >= SQUEEZE_MIN_VOL_24H_M:
                    LOG.info(
                        "hunt_squeeze_charged",
                        symbol=symbol,
                        bb_width_pctile=squeeze.get("bb_width_pctile_1h"),
                        donchian_pct=squeeze.get("donchian_width_pct_1h"),
                        oi_z=squeeze.get("oi_z"),
                        gls_z=squeeze.get("gls_z"),
                    )
                    if (
                        send_telegram
                        and broadcaster is not None
                        and _cooldown_ok(
                            symbol,
                            "squeeze",
                            state,
                            now=now,
                            minutes=SQUEEZE_COOLDOWN_MINUTES,
                        )
                    ):
                        result = await broadcaster.send_html(_format_squeeze_telegram(row))
                        if result.status == "sent":
                            state[f"{symbol}:squeeze"] = now.isoformat()
                            LOG.info(
                                "hunt_squeeze_telegram_sent",
                                symbol=symbol,
                                message_id=result.message_id,
                            )

                pend = notify_pending.get(symbol)
                if (
                    pend
                    and send_telegram
                    and broadcaster is not None
                    and not row.get("error")
                ):
                    ndir = str(pend.get("direction") or "short")
                    nsetup = dump if ndir == "short" else long_setup
                    lc_dict = lifecycle_raw if isinstance(lifecycle_raw, dict) else {}
                    nfuel = max(
                        float((nsetup or {}).get("dump_fuel") or 0),
                        float((nsetup or {}).get("long_fuel") or 0),
                        float((nsetup or {}).get("dump_score") or 0),
                        float((nsetup or {}).get("long_score") or 0),
                    )
                    nphase = str((nsetup or {}).get("phase") or "")
                    await_phase = str(pend.get("await_phase") or "dump_confirmed")
                    min_fuel = float(pend.get("min_fuel") or 70.0)
                    notify_on_forming = bool(pend.get("notify_on_forming"))
                    forming_phases = frozenset(
                        {
                            "dump_setup_forming",
                            "dump_imminent",
                            "dump_initiating",
                            "exhaustion_watch",
                        }
                    )
                    forming_ready = (
                        notify_on_forming
                        and nsetup
                        and not bool(nsetup.get("confirmed"))
                        and nfuel >= min_fuel
                        and nphase in forming_phases
                        and str(lc_dict.get("phase") or "")
                        in ("exhaustion_at_high", "distribution", "dump_active")
                    )
                    phase_ready = (
                        nsetup
                        and not bool(nsetup.get("confirmed"))
                        and nphase == await_phase
                        and nfuel >= min_fuel
                    )
                    if nsetup and bool(nsetup.get("confirmed")):
                        if not _should_alert(
                            nsetup,
                            direction=ndir,
                            symbol=symbol,
                            lifecycle=lifecycle_raw,
                            row=row,
                        ):
                            LOG.info(
                                "signal_notify_blocked",
                                symbol=symbol,
                                direction=ndir,
                                reason=_alert_block_reason(
                                    nsetup,
                                    direction=ndir,
                                    symbol=symbol,
                                    lifecycle=lifecycle_raw,
                                    row=row,
                                ),
                            )
                            clear_signal_notify(symbol)
                        else:
                            sym_label = html.escape(symbol.replace("USDT", "-USDT"))
                            body = _format_telegram(
                                row,
                                direction=ndir,
                                confirm_reasons=list(nsetup.get("confirm_hard") or []),
                            )
                            notify_msg = f"🔔 <b>/signal confirm</b> {sym_label}\n{body}"
                            notify_result = await broadcaster.send_html(notify_msg)
                            if notify_result.status == "sent":
                                clear_signal_notify(symbol)
                                LOG.info(
                                    "signal_notify_sent",
                                    symbol=symbol,
                                    direction=ndir,
                                    message_id=notify_result.message_id,
                                )
                    elif (forming_ready or phase_ready) and ndir == "short":
                        price_now = float(row.get("price") or 0)
                        if _entry_past_tp1(nsetup, direction=ndir, price=price_now):
                            LOG.info(
                                "signal_notify_skipped_past_tp1",
                                symbol=symbol,
                                direction=ndir,
                                price=price_now,
                            )
                        else:
                            tier: str = (
                                "likely" if bool(nsetup.get("confirmed")) else
                                "armed" if nfuel >= effective_hunt_params(symbol).confirm_min_score else
                                "prep"
                            )
                            skip = dump_hunt_skip_reason(
                                symbol=symbol,
                                tier=tier,  # type: ignore[arg-type]
                                price=price_now,
                                setup=nsetup,
                                lifecycle=lc_dict,
                                now=now,
                            )
                            if skip:
                                LOG.debug(
                                    "signal_notify_skipped",
                                    symbol=symbol,
                                    reason=skip,
                                    tier=tier,
                                )
                            else:
                                imp = row.get("impulse") or {}
                                notify_msg = format_dump_hunt_telegram(
                                    symbol=symbol,
                                    tier=tier,  # type: ignore[arg-type]
                                    price=price_now,
                                    setup=nsetup,
                                    lifecycle=lc_dict,
                                    chg_24h=float(row.get("chg_24h_pct") or 0),
                                    impulse_low=float(
                                        row.get("impulse_low")
                                        or imp.get("hunt_low")
                                        or 0
                                    ),
                                    atr15=float(
                                        ((row.get("timeframes") or {}).get("15m") or {}).get("atr14")
                                        or 0
                                    ),
                                    note=f"forming · {nphase} · fuel {nfuel:.0f}",
                                )
                                sent = await maybe_send_dump_hunt_telegram(
                                    broadcaster,
                                    symbol=symbol,
                                    tier=tier,  # type: ignore[arg-type]
                                    message=notify_msg,
                                    now=now,
                                    price=price_now,
                                    setup=nsetup,
                                    lifecycle=lc_dict,
                                )
                                if sent:
                                    LOG.info(
                                        "signal_notify_forming_sent",
                                        symbol=symbol,
                                        direction=ndir,
                                        tier=tier,
                                        phase=nphase,
                                        fuel=nfuel,
                                    )
                                    append_signal_event(
                                        "forming_notify",
                                        symbol=symbol,
                                        direction=ndir,
                                        detail=nphase,
                                        payload={"fuel": nfuel, "tier": tier},
                                    )

                if followups:
                    mark_followups_sent(tracker_state, followups, now=now)
                    for fu in followups:
                        if fu.event == "invalidate":
                            append_signal_event(
                                "invalidate",
                                symbol=fu.symbol,
                                direction=str((fu.payload or {}).get("direction") or ""),
                                detail=str(fu.detail or ""),
                                payload=fu.payload or {},
                            )
                    if pump_store is not None:
                        for fu in followups:
                            if fu.event == "fix_profit_tp1":
                                record_signal_outcome(
                                    pump_store, symbol=fu.symbol, outcome="tp1", now=now
                                )
                            elif fu.event == "fix_profit_tp2":
                                record_signal_outcome(
                                    pump_store, symbol=fu.symbol, outcome="tp2", now=now
                                )
                            elif fu.event == "invalidate":
                                record_signal_outcome(
                                    pump_store, symbol=fu.symbol, outcome="invalidate", now=now
                                )

                if send_telegram and broadcaster is not None and not row.get("error"):
                    for direction, setup in (("short", dump), ("long", long_setup)):
                        if not setup:
                            continue
                        if HUNT_SNIPER_MODE:
                            # H-A: only short fade in lifecycle `dump_active` ships live.
                            # Long stays shadow (tracked, never announced).
                            if direction != "short":
                                continue
                            _lc_phase = str((lifecycle_raw or {}).get("phase") or "")
                            if _lc_phase not in HUNT_SNIPER_LIVE_PHASES:
                                continue
                            # HMSTR-class squeeze guard (forensics 2026-06-12): do not
                            # ship a short while top traders are heavily long — fading
                            # smart-money positioning is squeeze fuel. On the 11 live
                            # signals top_ls_1h>=2.0 flagged the genuine bad entry
                            # (HMSTR 2.48) and EPIC (2.11); every winner with data <=1.91.
                            # Signal still tracked (shadow) for outcome data.
                            _top_ls = ((row.get("market") or {})).get("top_ls_1h")
                            if _top_ls is not None:
                                try:
                                    if float(_top_ls) >= HUNT_SNIPER_TOP_LS_MAX:
                                        continue
                                except (TypeError, ValueError):
                                    pass
                        if not _should_alert(
                            setup,
                            direction=direction,
                            symbol=symbol,
                            lifecycle=lifecycle_raw,
                            row=row,
                        ):
                            await _maybe_send_early_alert(
                                broadcaster,
                                symbol=symbol,
                                direction=direction,
                                setup=setup,
                                row=row,
                                lifecycle_raw=lifecycle_raw,
                                state=state,
                                mode=mode,
                                now=now,
                            )
                            lc = lifecycle_raw if isinstance(lifecycle_raw, dict) else {}
                            fuel = float(
                                setup.get("dump_fuel") or setup.get("long_fuel")
                                or setup.get("dump_score")
                                or setup.get("long_score")
                                or 0
                            )
                            if bool(setup.get("confirmed")):
                                gate = evaluate_alert_gate(
                                    setup,
                                    direction=direction,
                                    symbol=symbol,
                                    lifecycle=lifecycle_raw,
                                    row=row,
                                )
                                LOG.info(
                                    "watch_alert_blocked",
                                    symbol=symbol,
                                    direction=direction,
                                    score=setup.get("dump_score") or setup.get("long_score"),
                                    hunt_phase=lc.get("phase"),
                                    block_code=gate.code,
                                    reason=gate.message,
                                )
                                append_signal_event(
                                    "blocked",
                                    symbol=symbol,
                                    direction=direction,
                                    detail=gate.message,
                                    payload={
                                        "block_code": gate.code,
                                        "score": setup.get("dump_score")
                                        or setup.get("long_score"),
                                        "fuel": setup.get("dump_fuel")
                                        or setup.get("long_fuel"),
                                        "phase": setup.get("phase"),
                                        "lifecycle_phase": lc.get("phase"),
                                    },
                                )
                            elif fuel >= effective_hunt_params(symbol).forming_min_score:
                                LOG.info(
                                    "watch_setup_forming",
                                    symbol=symbol,
                                    direction=direction,
                                    fuel=fuel,
                                    phase=setup.get("phase"),
                                    lifecycle_phase=lc.get("phase"),
                                    confirmed=False,
                                )
                                append_signal_event(
                                    "forming",
                                    symbol=symbol,
                                    direction=direction,
                                    detail=setup.get("phase") or "",
                                    payload={
                                        "fuel": fuel,
                                        "lifecycle_phase": lc.get("phase"),
                                    },
                                )
                            continue
                        # Lifecycle phase overrides static pin: a confirmed short in a
                        # live dump (or long in a bounce) must not die to mode=long/short.
                        lc_phase = str((lifecycle_raw or {}).get("phase") or "")
                        if (
                            direction == "short"
                            and mode not in ("short", "both")
                            and lc_phase
                            not in ("dump_active", "exhaustion_at_high", "distribution")
                        ):
                            continue
                        if (
                            direction == "long"
                            and mode not in ("long", "both")
                            and lc_phase
                            not in (
                                "post_dump_bounce",
                                "accumulation",
                                "recovery",
                                "breakout_arming",
                                "impulse_initiating",
                            )
                        ):
                            continue
                        if not _cooldown_ok(symbol, direction, state, now=now):
                            continue
                        price_now = float(row.get("price") or 0)
                        if _entry_past_tp1(setup, direction=direction, price=price_now):
                            LOG.info(
                                "watch_telegram_skipped_past_tp1",
                                symbol=symbol,
                                direction=direction,
                                price=price_now,
                                tp1=setup.get("tp1"),
                            )
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
                            if pump_store is not None:
                                record_pump_signal_open(
                                    pump_store,
                                    symbol=symbol,
                                    direction=direction,
                                    now=now,
                                )
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
                                entry_message_id=result.message_id,
                                features_open=feature_vector_from_row(row),
                                book_walls=book_walls_from_row(row),
                            )
                            LOG.info(
                                "watch_telegram_sent",
                                symbol=symbol,
                                direction=direction,
                                message_id=result.message_id,
                            )
                            append_signal_event(
                                "confirmed",
                                symbol=symbol,
                                direction=direction,
                                detail="telegram_sent",
                                payload={
                                    "message_id": result.message_id,
                                    "score": setup.get("dump_score")
                                    or setup.get("long_score"),
                                    "phase": setup.get("phase"),
                                },
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
            except defensive_exc_types(asyncio.IncompleteReadError) as exc:
                LOG.warning("dump_symbol_failed", symbol=symbol, error=repr(exc))
                rows.append({"ts": now.isoformat(), "symbol": symbol, "error": repr(exc)})

        # Orphan reconciliation: active signals whose symbol left the watchlist
        # would otherwise never close (PLAYUSDT held TP2 for 18h unnoticed).
        orphan_events = await _reconcile_orphan_signals(
            client, tracker_state, seen_symbols=set(symbols), now=datetime.now(UTC)
        )
        if orphan_events:
            mark_followups_sent(tracker_state, orphan_events, now=datetime.now(UTC))
            for fu in orphan_events:
                LOG.info(
                    "watch_followup_orphan",
                    symbol=fu.symbol,
                    followup_event=fu.event,
                    detail=fu.detail,
                )
                if pump_store is not None:
                    if fu.event == "fix_profit_tp1":
                        record_signal_outcome(pump_store, symbol=fu.symbol, outcome="tp1", now=now)
                    elif fu.event == "fix_profit_tp2":
                        record_signal_outcome(pump_store, symbol=fu.symbol, outcome="tp2", now=now)
                    elif fu.event == "invalidate":
                        record_signal_outcome(
                            pump_store, symbol=fu.symbol, outcome="invalidate", now=now
                        )
                announced = bool((fu.payload or {}).get("announced", True))
                if send_telegram and broadcaster is not None and announced:
                    msg = _format_followup_telegram(fu, {"symbol": fu.symbol})
                    await broadcaster.send_html(msg)
        return rows
    finally:
        _save_state(state)
        save_tracker_state(tracker_state)
        save_prep_shadow_state(prep_shadow_state)


async def _run_loop(
    cli_symbols: tuple[str, ...],
    interval_s: int,
    once: bool,
    *,
    send_telegram: bool,
) -> None:
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    if migrate_calibration_split():
        LOG.info("hunt_calibration_migrated", path="hunt/data/hunt_calibration.json")
    try:
        rot_stats = rotate_hunt_ticks()
        if rot_stats.get("appended_lines") or rot_stats.get("archived"):
            LOG.info("hunt_tick_rotate", **rot_stats)
    except Exception:
        LOG.exception("hunt_tick_rotate_failed")
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
    ws_feed = HuntWsFeed(
        proxy_url=settings.network.proxy_url,
        trust_env=settings.network.trust_env,
    )
    spot_companion = SpotCompanionService(
        proxy_url=settings.network.proxy_url,
        trust_env=settings.network.trust_env,
    )
    ws_feed.set_symbols(list(cli_symbols))
    await ws_feed.start()
    # Persistent across ticks: kline/OI caches live in client; oi_flush/oi_build need prev tick.
    prev_oi: dict[str, float | None] = {}
    last_bias: dict[str, str] = {}
    ignition_state = load_ignition_state()
    pump_store = load_pump_history()
    adaptive_store = load_adaptive_store()
    if not pump_store.symbols and not pump_store.event_log:
        backfill_from_jsonl(pump_store)
        save_pump_history(pump_store)

    last_scan = 0.0
    last_regime = 0.0
    last_tick_rotate = time.monotonic()
    cached = load_regime_file()
    if cached is not None:
        apply_snapshot(cached)
    try:
        await refresh_market_regime(client)
        last_regime = time.monotonic()
    except Exception:
        LOG.exception("market_regime_startup_failed")

    # /signal command loop is independent of confirm-broadcast preflight.
    tg_cmds = build_hunt_telegram_commands(settings) if settings.tg_token else None
    tg_task: asyncio.Task[None] | None = None
    if tg_cmds is not None:
        tg_task = asyncio.create_task(tg_cmds.run_forever(), name="hunt_tg_commands")
        LOG.info("hunt_telegram_commands_scheduled")

    try:
        tick_ctx: dict[str, Any] | None = None
        while not _STOP:
            started = time.monotonic()
            try:
                if time.monotonic() - last_regime >= REGIME_REFRESH_S:
                    try:
                        snap = await refresh_market_regime(client)
                        last_regime = time.monotonic()
                        LOG.info(
                            "market_regime_tick",
                            regime=snap.regime,
                            anomaly_chg=snap.params.anomaly_min_chg_24h_pct,
                            n_liquid=snap.n_liquid,
                        )
                    except Exception:
                        LOG.exception("market_regime_refresh_failed")
                        last_regime = time.monotonic()

                if time.monotonic() - last_scan >= SCAN_INTERVAL_S:
                    try:
                        from hunt_watch.scanner_runner import run_scan

                        summary = await run_scan(limit=30, min_score=45.0)
                        LOG.info(
                            "hunt_scan_refresh",
                            watch=summary.get("watch_count"),
                            priority=summary.get("priority_count"),
                        )
                    except defensive_exc_types(asyncio.IncompleteReadError) as exc:
                        LOG.warning("hunt_scan_refresh_failed", error=repr(exc))
                    last_scan = time.monotonic()

                settings = load_settings()
                now = datetime.now(UTC)
                ticker_raw = await _safe_fetch(client.fetch_ticker_24h()) or []
                ticker_by_sym = {str(t.get("symbol")): t for t in ticker_raw if t.get("symbol")}
                new_ignitions, ignition_state = process_ticker_snapshots(
                    ticker_raw,
                    ignition_state,
                    now=now,
                    window_s=float(IGNITION_WINDOW_S),
                    min_pct=float(active_params().ignition_min_pct),
                    min_vol_delta_usd=float(IGNITION_MIN_VOL_DELTA_USD),
                    min_qvol_usd=float(active_params().ignition_min_qvol_usd),
                    ttl_s=float(IGNITION_TTL_S),
                    adaptive=adaptive_store,
                )
                save_ignition_state(ignition_state)
                save_adaptive_store(adaptive_store)
                if not IGNITION_TELEGRAM_ENABLED:
                    for ig in ignition_state.active.values():
                        ig.notified = True
                ignition_by_sym = {
                    sym: ig.to_row() for sym, ig in ignition_state.active.items()
                }
                for ev in new_ignitions:
                    LOG.info(
                        "hunt_ignition",
                        symbol=ev.symbol,
                        direction=ev.direction,
                        price_delta_pct=round(ev.price_delta_pct, 2),
                        vol_delta_usd=round(ev.vol_delta_usd, 0),
                        window_s=round(ev.window_s, 1),
                    )
                    tick = ticker_by_sym.get(ev.symbol) or {}
                    ign_price = float(tick.get("last_price") or 0)
                    if ign_price > 0:
                        record_pump_leg(
                            pump_store,
                            symbol=ev.symbol,
                            kind=ev.direction,
                            source="ignition",
                            price=ign_price,
                            change_24h_pct=float(tick.get("price_change_percent") or 0),
                            now=now,
                        )
                price_map = {
                    sym: float(row.get("last_price") or 0)
                    for sym, row in ticker_by_sym.items()
                    if float(row.get("last_price") or 0) > 0
                }
                observe_prices(pump_store, price_map, now=now)
                pump_stats_by_sym = {
                    sym: st.to_public() for sym, st in pump_store.symbols.items()
                }
                if send_telegram and broadcaster is not None and IGNITION_TELEGRAM_ENABLED:
                    for ig in pending_ignition_alerts(ignition_state):
                        hist = format_history_telegram(stats_for(pump_store, ig.symbol))
                        msg = format_ignition_telegram(ig)
                        if hist:
                            msg = f"{msg}\n<i>{html.escape(hist)}</i>"
                        result = await broadcaster.send_html(msg)
                        if result.status == "sent":
                            mark_ignition_notified(ignition_state, ig.symbol)
                            save_ignition_state(ignition_state)
                            LOG.info(
                                "hunt_ignition_telegram_sent",
                                symbol=ig.symbol,
                                direction=ig.direction,
                                message_id=result.message_id,
                            )
                        else:
                            LOG.warning(
                                "hunt_ignition_telegram_failed",
                                symbol=ig.symbol,
                                status=result.status,
                                reason=result.reason,
                            )

                symbols, mode_map = resolve_watch_universe(
                    settings,
                    static_modes=SYMBOL_WATCH_MODES,
                    ignited=ignition_by_sym,
                )
                merged: list[str] = list(symbols)
                for sym in cli_symbols:
                    s = sym.upper()
                    if s not in merged:
                        merged.append(s)
                    mode_map.setdefault(s, SYMBOL_WATCH_MODES.get(s, "short"))
                active = tuple(dict.fromkeys(merged))
                ws_feed.set_symbols(list(active))
                ws_n = min(len(active), 24) + 1
                if ws_feed.kline_5m_enabled:
                    ws_n += min(len(active), 24)
                LOG.info(
                    "watch_universe",
                    symbols=len(active),
                    ignited=len(ignition_by_sym),
                    ws_streams=ws_n,
                    kline_5m=ws_feed.kline_5m_enabled,
                    list=list(active)[:8],
                )

                tick_ctx = {
                    "active": active,
                    "settings": settings,
                    "minimums": minimums,
                    "client": client,
                    "prev_oi": prev_oi,
                    "last_bias": last_bias,
                    "mode_map": mode_map,
                    "broadcaster": broadcaster,
                    "send_telegram": send_telegram,
                    "ticker_by_sym": ticker_by_sym,
                    "ignition_by_sym": ignition_by_sym,
                    "pump_stats_by_sym": pump_stats_by_sym,
                    "pump_store": pump_store,
                    "ws_feed": ws_feed,
                    "spot_companion": spot_companion,
                }
                rows = await _run_tick(active, **{k: v for k, v in tick_ctx.items() if k != "active"})
                save_pump_history(pump_store)
                with OUT_PATH.open("a", encoding="utf-8") as fh:
                    for row in rows:
                        fh.write(json.dumps(row, default=str) + "\n")
                if (
                    OUT_PATH.exists()
                    and OUT_PATH.stat().st_size >= TICK_ROTATE_MIN_BYTES
                    and time.monotonic() - last_tick_rotate >= TICK_ROTATE_INTERVAL_S
                ):
                    try:
                        rot_stats = rotate_hunt_ticks()
                        if rot_stats.get("appended_lines") or rot_stats.get("archived"):
                            LOG.info("hunt_tick_rotate_periodic", **rot_stats)
                        last_tick_rotate = time.monotonic()
                    except Exception:
                        LOG.exception("hunt_tick_rotate_periodic_failed")
                if once:
                    print(json.dumps(rows, indent=2, default=str))
                    break
            except Exception:
                LOG.exception("dump_watch_tick_error")
                if once:
                    raise
            if once:
                break
            deadline = started + max(1.0, float(interval_s))
            while time.monotonic() < deadline and not _STOP:
                pending = ws_feed.pop_kline_close_triggers()
                if pending and tick_ctx is not None:
                    ctx = tick_ctx
                    fast_syms = tuple(s for s in ctx["active"] if s in pending)
                    if fast_syms:
                        LOG.info("watch_kline_5m_trigger", symbols=list(fast_syms))
                        try:
                            fast_rows = await _run_tick(
                                fast_syms,
                                **{k: v for k, v in ctx.items() if k != "active"},
                            )
                            for row in fast_rows:
                                row["tick_trigger"] = "kline_5m"
                            with OUT_PATH.open("a", encoding="utf-8") as fh:
                                for row in fast_rows:
                                    fh.write(json.dumps(row, default=str) + "\n")
                        except Exception:
                            LOG.exception("watch_kline_fast_tick_failed")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                await asyncio.sleep(min(3.0, remaining))
    finally:
        if tg_task is not None:
            tg_task.cancel()
            try:
                await tg_task
            except asyncio.CancelledError:
                pass
        if tg_cmds is not None:
            await tg_cmds.close()
        await ws_feed.stop()
        await spot_companion.close()
        await client.close()
        if broadcaster is not None:
            await broadcaster.close()


def _acquire_single_instance_lock() -> None:
    """Refuse to start if another live watcher holds the lock.

    Concurrent watchers write the shared signal_state/history and produce
    duplicate / opened_at=None rows (the multi-watcher race). One writer only.
    """
    from hunt_watch.paths import DATA

    lock = DATA / "watch.pid"
    if lock.exists():
        try:
            other = int(lock.read_text(encoding="utf-8").strip() or "0")
        except (OSError, ValueError):
            other = 0
        if other and other != os.getpid():
            alive = False
            try:
                os.kill(other, 0)  # liveness probe — raises if dead
                alive = True
            except ProcessLookupError:
                alive = False  # stale lock, take it over
            except PermissionError:
                alive = True  # exists but not ours
            if alive:
                raise SystemExit(
                    f"watch.py already running (pid={other}); refusing to start a second writer. "
                    f"Kill it first or remove {lock} if stale."
                )
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text(str(os.getpid()), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Signal minute watch + Telegram")
    parser.add_argument(
        "--symbols",
        nargs="*",
        default=list(DEFAULT_SYMBOLS),
        help="CLI extras on top of anchors BTC ETH XAU XAG + scanner watchlist",
    )
    parser.add_argument("--interval", type=int, default=60)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--no-telegram", action="store_true", help="Log only, no Telegram sends")
    args = parser.parse_args()
    symbols = tuple(dict.fromkeys(s.upper() for s in args.symbols))
    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)
    if not args.once:
        _acquire_single_instance_lock()  # one writer only — prevents the multi-watcher race
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
