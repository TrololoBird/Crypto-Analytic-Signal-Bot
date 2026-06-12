"""Per-symbol tick collection — REST/WS snapshot assembly (H-B rewrite)."""

from __future__ import annotations

import asyncio
import html
from datetime import UTC, datetime
from typing import Any, Literal

from hunt_watch.data_completeness import DataIncompleteError, series_z_strict
from hunt_watch.directional_filters import directional_filters
from hunt_watch.feature_latch import book_walls_from_depth
from hunt_watch.frame_fallback import patch_work_4h, should_use_young_lite_path
from hunt_watch.indicators import rsi14_from_ohlc
from hunt_watch.levels import fib_retracement_levels, structural_long_levels, structural_short_levels
from hunt_watch.lifecycle import (
    apply_short_invalidation,
    assess_hunt_lifecycle,
    effective_support_break,
)
from hunt_watch.lifecycle_sticky import stabilize as stabilize_lifecycle
from hunt_watch.liquidity_gate import liquidity_skip_reason
from hunt_watch.param_store import effective_hunt_params
from hunt_watch.pump_history import score_bonus
from hunt_watch.session_state import merge_hunt_extremes
from hunt_watch.signal_engine import (
    confirm_dump as _se_confirm_dump,
    confirm_long as _se_confirm_long,
    enrich_dump_setup,
    enrich_long_setup,
    phase_dump as _se_phase_dump,
    phase_long as _se_phase_long,
)
from hunt_watch.targets import PINNED_SYMBOLS

from hunt_core.market import HuntCcxtClient, HuntCcxtSpotCompanion, HuntCcxtStreams
from hunt_core.data.rest_tiers import SnapshotTier, resolve_kline_map, rest_pack_specs, ws_orderflow_fresh

from hunt_core.data_readiness import kline_fetch_limit
from hunt_core.domain.schemas import SymbolFrames, UniverseSymbol
from hunt_core.errors import DEFENSIVE_EXC
from hunt_core.features.pivots import _pivot_rows, with_spec_columns
from hunt_core.features.prepare import _prepare_frame, prepare_symbol
from hunt_core.market.book_parsers import depth_imbalance_from_book, microprice_bias_from_book

WatchMode = Literal["short", "long", "both"]

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
IMPULSE_WINDOW_ALT_4H = 12
IMPULSE_WINDOW_ALT_1H = 48

def kline_limits(minimums: dict[str, int]) -> dict[str, int]:
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
async def safe_fetch(coro: Any) -> Any:
    try:
        return await coro
    except DEFENSIVE_EXC:
        return None


async def _fetch_rest_pack(
    client: HuntCcxtClient,
    symbol: str,
    *,
    tier: SnapshotTier = "full",
    ws_feed: HuntCcxtStreams | None = None,
) -> dict[str, Any]:
    """Fetch public REST enrichment; fast tier keeps dump-onset fields."""
    ws_snap = ws_feed.snapshot(symbol) if ws_feed is not None else None
    specs = rest_pack_specs(
        client,
        symbol,
        tier=tier,
        ws_orderflow_fresh=ws_orderflow_fresh(ws_snap),
    )
    results = await asyncio.gather(*(c for _, c in specs), return_exceptions=True)
    pack: dict[str, Any] = {}
    for (name, _), res in zip(specs, results, strict=True):
        pack[name] = None if isinstance(res, BaseException) else res
    depth = pack.get("book_depth")
    if not isinstance(depth, dict) or not depth.get("bid_price"):
        pack["book_ticker"] = await safe_fetch(client._fetch_book_ticker_rest_detail(symbol))
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
    import polars as pl

    sym_close = sym_work_1h["close"].tail(lookback + 1).cast(pl.Float64)
    btc_close = btc_work_1h["close"].tail(lookback + 1).cast(pl.Float64)
    sym_r = sym_close.pct_change().drop_nulls()
    btc_r = btc_close.pct_change().drop_nulls()
    n = min(sym_r.len(), btc_r.len())
    if n < 8:
        return None
    corr = sym_r.tail(n).corr(btc_r.tail(n))
    return round(float(corr), 4) if corr is not None else None


def _btc_beta_1h(sym_work_1h: Any, btc_work_1h: Any, *, lookback: int = 48) -> float | None:
    """Rolling OLS beta of symbol vs BTC 1h returns via polars_ols."""
    try:
        import polars_ols  # noqa: PLC0415
    except ImportError:
        return None
    if (
        sym_work_1h is None
        or btc_work_1h is None
        or sym_work_1h.is_empty()
        or btc_work_1h.is_empty()
        or sym_work_1h.height < lookback + 2
        or btc_work_1h.height < lookback + 2
    ):
        return None
    sym_r = sym_work_1h["close"].tail(lookback + 1).cast(pl.Float64).pct_change().drop_nulls()
    btc_r = btc_work_1h["close"].tail(lookback + 1).cast(pl.Float64).pct_change().drop_nulls()
    n = min(sym_r.len(), btc_r.len())
    if n < 8:
        return None
    tmp = pl.DataFrame({"y": sym_r.tail(n), "x": btc_r.tail(n)})
    try:
        result = polars_ols.compute_least_squares(tmp["y"], features=[tmp["x"]], add_intercept=True)
        beta = float(result.get_column("x")[0])
        return round(beta, 4)
    except Exception:
        return None


def _apply_rest_enrichments(
    prepared: Any,
    *,
    client: HuntCcxtClient,
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
        "btc_beta_1h": getattr(prepared, "btc_beta_1h", None),
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

def _col(df: Any, name: str, default: float = 0.0, *, idx: int = -1) -> float:
    if df is None or df.is_empty() or name not in df.columns:
        return default
    try:
        return float(df.item(idx, name))
    except TypeError, ValueError, IndexError:
        return default


def _merge_ws_kline_closed(
    tf: dict[str, Any],
    symbol: str,
    ws_feed: HuntCcxtStreams | None,
    *,
    tf_key: str = "1m_closed",
) -> None:
    """Overlay WS grace-closed kline bar onto REST closed TF (lower staleness)."""
    if ws_feed is None:
        return
    overlay = ws_feed.closed_kline_overlay(symbol)
    if not overlay:
        return
    base = tf.get(tf_key)
    if not isinstance(base, dict) or base.get("status") == "empty":
        tf[tf_key] = overlay
        return
    tf[tf_key] = {**base, **overlay}


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


def squeeze_watch(tf: dict[str, Any], market: dict[str, Any]) -> dict[str, Any] | None:
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


def format_squeeze_telegram(row: dict[str, Any]) -> str:
    from hunt_watch.deliver.telegram import format_squeeze_telegram as _fmt  # noqa: PLC0415
    return _fmt(row)
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
    tier: SnapshotTier = "full",
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


def _phase_long(long_setup: dict[str, Any], confirmed: bool, *, symbol: str = "") -> str:
    return _se_phase_long(long_setup, confirmed, cal=effective_hunt_params(symbol))


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


async def snapshot_symbol(
    client: HuntCcxtClient,
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
    ws_feed: HuntCcxtStreams | None = None,
    spot_companion: HuntCcxtSpotCompanion | None = None,
    stagger_klines_ms: int = 0,
    pump_stats: dict[str, Any] | None = None,
    tier: SnapshotTier = "full",
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
    limits = kline_limits(minimums)
    if stagger_klines_ms > 0 and tier == "full":
        tf_order = ("1m", "3m", "5m", "15m", "1h", "4h", "1d")
        kline_map: dict[str, Any] = {}
        for name in tf_order:
            res = await safe_fetch(
                client.fetch_klines_cached(symbol, name, limit=limits[name])
            )
            kline_map[name] = res
            await asyncio.sleep(stagger_klines_ms / 1000.0)
    else:
        kline_map = await resolve_kline_map(
            client, symbol, limits, tier=tier, safe_fetch=safe_fetch
        )
    df_1m = kline_map["1m"]
    if df_1m is None or df_1m.is_empty():
        return {"ts": datetime.now(UTC).isoformat(), "symbol": symbol, "error": "klines_1m_failed"}
    df_5m = kline_map["5m"]
    pack = await _fetch_rest_pack(client, symbol, tier=tier, ws_feed=ws_feed)
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
        beta = _btc_beta_1h(prepared.work_1h, btc_work_1h)
        if beta is not None:
            prepared.btc_beta_1h = beta

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
    _merge_ws_kline_closed(tf, symbol, ws_feed)
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
        "snapshot_tier": tier,
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
        "squeeze": squeeze_watch(tf, market),
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


__all__ = [
    "WatchMode",
    "safe_fetch",
    "snapshot_symbol",
    "format_squeeze_telegram",
    "squeeze_watch",
]
