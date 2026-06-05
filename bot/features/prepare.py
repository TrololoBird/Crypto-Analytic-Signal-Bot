"""Technical analysis feature preparation (Polars-native runtime path).

Indicators stay Polars-native with optional `polars_ta` for EMA/ROC/OBV.
Pure-Polars formulas are canonical for Wilder RSI/ATR/ADX, MACD, BB (ddof=1), and structure.
"""

from __future__ import annotations

import logging
import math
import threading
from collections import OrderedDict
from typing import Any, cast

import polars as pl
import structlog

from bot.runtime.errors import DEFENSIVE_EXC

from ..domain.schemas import PreparedSymbol, SymbolFrames, UniverseSymbol
from ..market.ws import depth_imbalance_from_book, microprice_bias_from_book
from ..runtime_policy import (
    configured_context_timeframes,
    configured_primary_timeframe,
)
from .microstructure import add_microstructure_features
from .prepare_frame import (
    _add_advanced_indicators,
    _as_optional_float,
    _numeric_item,
    _prepare_frame,
    _tail_value_signature,
    _timestamp_ns,
    add_session_cvd,
    has_minimum_bars,
    min_required_bars,
    take_frame_indicator_fallbacks,
)

LOG = structlog.get_logger("bot.features.prepare")

# Frame-level indicator cache - LRU with unique frame-window keys.
# ---------------------------------------------------------------------------

_MAX_CACHE_ENTRIES = 500
_FrameCacheValue = float | None
_FrameCacheKey = tuple[
    str, str, int, int, int, tuple[_FrameCacheValue, ...], tuple[object, ...] | None
]
_FRAME_CACHE_TAIL_COLUMNS = (
    "open",
    "high",
    "low",
    "close",
    "volume",
    "quote_volume",
    "num_trades",
    "taker_buy_base_volume",
    "taker_buy_quote_volume",
)
REQUIRED_COLS = {"open", "high", "low", "close", "volume"}
_CRITICAL_SIGNAL_COLS = frozenset(
    {
        "close",
        "open",
        "high",
        "low",
        "volume",
        "atr14",
        "rsi14",
        "adx14",
        "ema20",
        "ema50",
        "ema200",
    }
)


class _FrameCache:
    """Best-effort LRU cache for prepared frames.

    Keys include symbol, interval, row count, first/last close time, and the
    latest OHLCV-like values. `_prepare_frame` depends on the whole history
    window, and live partial kline updates keep the same close time while the
    current candle values change.

    Cache access never waits for a contended lock. Missing a cache hit is
    cheaper than blocking the async analysis loop behind another frame update.
    """

    __slots__ = ("_hits", "_lock", "_max_size", "_misses", "_store")

    def __init__(self, max_size: int = 500) -> None:
        self._store: OrderedDict[_FrameCacheKey, pl.DataFrame] = OrderedDict()
        self._max_size = max_size
        self._lock = threading.Lock()
        self._hits = 0
        self._misses = 0

    def get(self, key: _FrameCacheKey) -> pl.DataFrame | None:
        if not self._lock.acquire(blocking=False):
            self._misses += 1
            return None
        try:
            if key not in self._store:
                self._misses += 1
                return None
            self._store.move_to_end(key)
            self._hits += 1
            return self._store[key]
        finally:
            self._lock.release()

    def put(self, key: _FrameCacheKey, value: pl.DataFrame) -> None:
        if not self._lock.acquire(blocking=False):
            return
        try:
            if key in self._store:
                self._store.move_to_end(key)
            self._store[key] = value
            while len(self._store) > self._max_size:
                self._store.popitem(last=False)
        finally:
            self._lock.release()

    def stats(self) -> dict[str, float | int]:
        with self._lock:
            hits = int(self._hits)
            misses = int(self._misses)
            total = hits + misses
            return {
                "hits": hits,
                "misses": misses,
                "size": len(self._store),
                "hit_rate": round(hits / total, 6) if total else 0.0,
            }

    def cache_stats(self) -> dict[str, float | int]:
        return self.stats()


# Module-level singleton kept for backward compatibility.
_FRAME_CACHE = _FrameCache(max_size=_MAX_CACHE_ENTRIES)


def cache_stats() -> dict[str, float | int]:
    """Return frame preparation cache hit/miss counters for health telemetry."""
    return _FRAME_CACHE.stats()


def _bias_4h(work_4h: pl.DataFrame) -> str:
    """Determine 4h bias from EMA alignment."""
    if work_4h.is_empty():
        return "neutral"

    last = work_4h.row(-1, named=True)
    close = last["close"]
    ema20 = last["ema20"]
    ema50 = last["ema50"]
    ema200 = last["ema200"]

    if close > ema50 > ema200 and ema20 > ema50:
        return "uptrend"
    if close < ema50 < ema200 and ema20 < ema50:
        return "downtrend"
    return "neutral"


def _bias_1h(work_1h: pl.DataFrame) -> str:
    """Determine 1h bias from EMA alignment for 15M signal context."""
    if work_1h.is_empty():
        return "neutral"

    last = work_1h.row(-1, named=True)
    close = last["close"]
    ema20 = last["ema20"]
    ema50 = last["ema50"]
    ema200 = last["ema200"]

    if close > ema50 > ema200 and ema20 > ema50:
        return "uptrend"
    if close < ema50 < ema200 and ema20 < ema50:
        return "downtrend"
    return "neutral"


def _market_regime(
    work_4h: pl.DataFrame,
    work_1h: pl.DataFrame | None = None,
    work_15m: pl.DataFrame | None = None,
    threshold_choppy: float = 15.0,
    threshold_trending: float = 25.0,
) -> str:
    """Classify regime from 4h strength plus 1h/15m structure."""
    if work_4h.is_empty() or "adx14" not in work_4h.columns:
        return "neutral"

    adx_4h = _numeric_item(work_4h, -1, "adx14")
    bias_4h = _bias_4h(work_4h)
    regime_1h = _regime_1h_confirmed(work_1h if work_1h is not None else pl.DataFrame())
    atr_pct_15m = _numeric_item(work_15m if work_15m is not None else pl.DataFrame(), -1, "atr_pct")

    if (
        adx_4h >= threshold_trending
        and bias_4h in {"uptrend", "downtrend"}
        and regime_1h in {"uptrend", "downtrend"}
    ):
        return "trending"
    if adx_4h < threshold_choppy and regime_1h == "ranging":
        return "choppy"
    if atr_pct_15m >= 3.0 and regime_1h == "ranging":
        return "choppy"
    return "neutral"


# ---------------------------------------------------------------------------
# Structure-based helpers
# ---------------------------------------------------------------------------


def _swing_points(
    work: pl.DataFrame,
    n: int = 3,
    *,
    include_unconfirmed_tail: bool = False,
) -> tuple[pl.Series, pl.Series]:
    """Detect live-safe swing highs and lows without right-side lookahead.

    A pivot is confirmed only after the following bar has closed:
    ``high[i-n:i] < high[i]`` and ``high[i] > high[i+1]`` for highs,
    mirrored for lows. The implementation walks left-to-right and marks the
    pivot bar only when the confirmation bar is already present in ``work``.
    It deliberately avoids negative shifts / right-side rolling windows so a
    strategy cannot see a swing before it would have been known live.
    """
    if work.is_empty():
        return (
            pl.Series("swing_high", [], dtype=pl.Boolean),
            pl.Series("swing_low", [], dtype=pl.Boolean),
        )
    if "high" not in work.columns or "low" not in work.columns:
        return (
            pl.Series("swing_high", [False] * work.height, dtype=pl.Boolean),
            pl.Series("swing_low", [False] * work.height, dtype=pl.Boolean),
        )

    lookback = max(1, int(n))
    highs = [float(value) if value is not None else float("nan") for value in work["high"]]
    lows = [float(value) if value is not None else float("nan") for value in work["low"]]
    swing_high_values = [False] * work.height
    swing_low_values = [False] * work.height

    def _finite(values: list[float]) -> bool:
        return all(math.isfinite(value) for value in values)

    # confirm_idx is the just-closed candle that confirms pivot_idx.
    for confirm_idx in range(lookback + 1, work.height):
        pivot_idx = confirm_idx - 1
        left_start = pivot_idx - lookback
        left_highs = highs[left_start:pivot_idx]
        left_lows = lows[left_start:pivot_idx]
        pivot_high = highs[pivot_idx]
        pivot_low = lows[pivot_idx]
        confirm_high = highs[confirm_idx]
        confirm_low = lows[confirm_idx]

        if _finite([*left_highs, pivot_high, confirm_high]):
            swing_high_values[pivot_idx] = (
                pivot_high > max(left_highs) and pivot_high > confirm_high
            )
        if _finite([*left_lows, pivot_low, confirm_low]):
            swing_low_values[pivot_idx] = pivot_low < min(left_lows) and pivot_low < confirm_low

    if include_unconfirmed_tail and work.height > lookback:
        tail_idx = work.height - 1
        left_highs = highs[tail_idx - lookback : tail_idx]
        left_lows = lows[tail_idx - lookback : tail_idx]
        tail_high = highs[tail_idx]
        tail_low = lows[tail_idx]
        if _finite([*left_highs, tail_high]):
            swing_high_values[tail_idx] = tail_high > max(left_highs)
        if _finite([*left_lows, tail_low]):
            swing_low_values[tail_idx] = tail_low < min(left_lows)

    return (
        pl.Series("swing_high", swing_high_values, dtype=pl.Boolean),
        pl.Series("swing_low", swing_low_values, dtype=pl.Boolean),
    )


def _market_structure_1h(work_1h: pl.DataFrame) -> str:
    """Determine 1h market structure from swing points."""
    if len(work_1h) < 20:
        return "ranging"

    swing_high, swing_low = _swing_points(work_1h, n=3)

    # Get swing high/low values
    last_highs = work_1h.filter(swing_high)["high"].tail(2)
    last_lows = work_1h.filter(swing_low)["low"].tail(2)

    if last_highs.len() < 2 or last_lows.len() < 2:
        return "ranging"

    hh = last_highs[1] > last_highs[0]  # higher high
    hl = last_lows[1] > last_lows[0]  # higher low
    lh = last_highs[1] < last_highs[0]  # lower high
    ll = last_lows[1] < last_lows[0]  # lower low

    if hh and hl:
        return "uptrend"
    if lh and ll:
        return "downtrend"
    return "ranging"


def _regime_4h_confirmed(work_4h: pl.DataFrame, min_bars: int = 3) -> str:
    """Strict 4h regime requiring consecutive bars in same trend."""
    if len(work_4h) < min_bars:
        return "ranging"

    tail = work_4h.tail(min_bars)

    # Check uptrend condition
    uptrend_count = tail.filter(
        (pl.col("ema20") > pl.col("ema50")) & (pl.col("ema50") > pl.col("ema200"))
    ).height

    # Check downtrend condition
    downtrend_count = tail.filter(
        (pl.col("ema20") < pl.col("ema50")) & (pl.col("ema50") < pl.col("ema200"))
    ).height

    if uptrend_count == min_bars:
        return "uptrend"
    if downtrend_count == min_bars:
        return "downtrend"
    return "ranging"


def _regime_1h_confirmed(work_1h: pl.DataFrame, min_bars: int = 3) -> str:
    """Strict 1h regime requiring consecutive bars in same trend for 15M signal context."""
    if len(work_1h) < min_bars:
        return "ranging"

    tail = work_1h.tail(min_bars)

    # Check uptrend condition
    uptrend_count = tail.filter(
        (pl.col("ema20") > pl.col("ema50")) & (pl.col("ema50") > pl.col("ema200"))
    ).height

    # Check downtrend condition
    downtrend_count = tail.filter(
        (pl.col("ema20") < pl.col("ema50")) & (pl.col("ema50") < pl.col("ema200"))
    ).height

    if uptrend_count == min_bars:
        return "uptrend"
    if downtrend_count == min_bars:
        return "downtrend"
    return "ranging"


def _volume_poc(work: pl.DataFrame, lookback: int = 96, buckets: int = 20) -> float | None:
    """Simplified Volume Point of Control (vectorized)."""
    if len(work) < 10:
        return None

    tail = work.tail(lookback)
    price_min = _as_optional_float(tail["low"].min())
    price_max = _as_optional_float(tail["high"].max())

    if price_min is None or price_max is None or price_max <= price_min:
        return price_max

    bucket_size = (price_max - price_min) / buckets

    # We need to distribute bar volume across all buckets the bar covers.
    # This is slightly more complex to vectorize than simple close-based POC.
    # For a simplified vectorized version, we'll use the bar midpoint for bucketing.
    midpoints = (tail["high"] + tail["low"]) / 2.0
    v_buckets = ((midpoints - price_min) / bucket_size).floor().cast(pl.Int32).clip(0, buckets - 1)

    vol_by_bucket = (
        pl.DataFrame({"b": v_buckets, "v": tail["volume"]}).group_by("b").agg(pl.col("v").sum())
    )

    if vol_by_bucket.is_empty():
        return price_min

    poc_bucket = int(vol_by_bucket.sort("v", descending=True).row(0)[0])
    return float(price_min + (poc_bucket + 0.5) * bucket_size)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


_EXPECTED_ZERO_COLUMNS = frozenset(
    {
        "macd_hist",
        "obv",
        "obv_ema20",
        "obv_above_ema",
        "squeeze_on",
        "squeeze_off",
        "squeeze_no",
        "squeeze_hist",
        "psar_reversal",
        "chandelier_dir",
        "session_asia",
        "session_london",
        "session_ny",
        "session_overlap",
        "session_asia_vol_20",
        "session_london_vol_20",
        "session_ny_vol_20",
        "session_overlap_vol_20",
        "delta_ratio",
        "roc10",
        "realized_vol_20",
        "vwap_deviation_z20",
        "aggression_shift",
        "depth_imbalance",
        "microprice_bias",
        "depth_wall_pressure",
        "tob_imbalance",
        "microprice_deviation_pct",
    }
)


def _series_numeric_bounds(series: pl.Series) -> tuple[float | None, float | None]:
    numeric = series.cast(pl.Float64, strict=False).drop_nulls()
    if numeric.is_empty():
        return None, None
    try:
        return _as_optional_float(numeric.min()), _as_optional_float(numeric.max())
    except (TypeError, ValueError):
        return None, None


def _sanity_check_prepared_frame(work: pl.DataFrame, symbol: str, interval: str) -> list[str]:
    """Return frame quality defects that must block signal preparation.

    Any non-empty list means the symbol cannot be analyzed safely: missing OHLCV,
    empty frame, null/NaN/inf on the signal bar, or impossible indicator ranges.
    """
    defects: list[str] = []
    missing = REQUIRED_COLS - set(work.columns)
    if missing:
        defects.append(f"Missing required columns: {missing}")
    if work.is_empty():
        defects.append(f"{symbol}/{interval}: prepared frame is empty")
        return defects

    signal_bar_cols = ("close", "atr14", "rsi14", "adx14")
    for column in signal_bar_cols:
        if column not in work.columns:
            continue
        raw = work[column][-1]
        if raw is None:
            defects.append(f"{column}: last bar is null")
            continue
        try:
            value = float(raw)
        except (TypeError, ValueError):
            defects.append(f"{column}: last bar non-numeric ({raw!r})")
            continue
        if not math.isfinite(value):
            defects.append(f"{column}: last bar non-finite ({value})")

    def _range_defect(column: str, low: float, high: float) -> None:
        if column not in work.columns:
            return
        min_value, max_value = _series_numeric_bounds(work[column])
        if min_value is None or max_value is None:
            defects.append(f"{column}: all values null or non-numeric")
            return
        if min_value < low or max_value > high:
            defects.append(
                f"{column}: out of range [{low}, {high}] min={min_value:.6f} max={max_value:.6f}"
            )

    def _positive_defect(column: str, *, allow_zero: bool) -> None:
        if column not in work.columns:
            return
        min_value, _ = _series_numeric_bounds(work[column])
        if min_value is None:
            defects.append(f"{column}: all values null or non-numeric")
            return
        if allow_zero:
            if min_value < 0.0:
                defects.append(f"{column}: negative value detected min={min_value:.6f}")
        elif min_value <= 0.0:
            defects.append(f"{column}: non-positive value detected min={min_value:.6f}")

    _range_defect("rsi14", 0.0, 100.0)
    _range_defect("adx14", 0.0, 100.0)
    _positive_defect("atr14", allow_zero=False)
    _positive_defect("ema20", allow_zero=False)
    _positive_defect("ema50", allow_zero=False)
    _positive_defect("ema200", allow_zero=False)
    _positive_defect("volume_ratio20", allow_zero=True)
    _positive_defect("close", allow_zero=False)

    for column in work.columns:
        if column.startswith("_") or column not in _CRITICAL_SIGNAL_COLS:
            continue
        series = work[column]
        if series.null_count() == work.height:
            defects.append(f"{column}: column is entirely null")
            continue
        if column in _EXPECTED_ZERO_COLUMNS:
            continue
        dtype = work.schema.get(column)
        if dtype is None or not (
            getattr(dtype, "is_numeric", lambda: False)() or dtype == pl.Boolean
        ):
            continue
    return defects


def _sanity_check_all_frames(prepared: PreparedSymbol) -> dict[str, list[str]]:
    """Run prepared-frame sanity checks for every available timeframe."""
    frames: dict[str, pl.DataFrame | None] = {
        "5m": prepared.work_5m,
        "15m": prepared.work_15m,
        "1h": prepared.work_1h,
        "4h": prepared.work_4h,
    }
    report: dict[str, list[str]] = {}
    for interval, frame in frames.items():
        if frame is None:
            continue
        warnings = _sanity_check_prepared_frame(frame, prepared.symbol, interval)
        if warnings:
            report[interval] = warnings
    return report


def _cached_prepare_frame(
    frame: pl.DataFrame,
    *,
    symbol: str = "",
    interval: str = "",
    cache: _FrameCache | None = None,
    ws_manager: Any | None = None,
    fallback_book: tuple[float | None, float | None, float | None, float | None] | None = None,
) -> pl.DataFrame:
    """_prepare_frame with LRU cache keyed on (symbol, interval, close_time)."""
    if frame.is_empty() or "close_time" not in frame.columns or "close" not in frame.columns:
        result = _enrich_with_ws_data(
            _prepare_frame(frame),
            symbol,
            ws_manager if interval == "15m" else None,
            fallback_book=fallback_book if interval == "15m" else None,
        )
        for warning in _sanity_check_prepared_frame(result, symbol, interval):
            LOG.warning(
                "prepared frame quality defect | symbol=%s interval=%s defect=%s",
                symbol,
                interval,
                warning,
            )
        return result

    last = frame.row(-1, named=True)
    first = frame.row(0, named=True)
    try:
        first_close_time_ns = _timestamp_ns(first["close_time"])
        close_time_ns = _timestamp_ns(last["close_time"])
    except (KeyError, TypeError, ValueError, OverflowError):
        result = _prepare_frame(frame)
        for warning in _sanity_check_prepared_frame(result, symbol, interval):
            LOG.warning(
                "prepared frame quality defect | symbol=%s interval=%s defect=%s",
                symbol,
                interval,
                warning,
            )
        return result

    tail_signature = _tail_value_signature(last)
    key = (
        symbol,
        interval,
        frame.height,
        first_close_time_ns,
        close_time_ns,
        tail_signature,
        _ws_enrichment_signature(symbol, ws_manager if interval == "15m" else None),
    )
    target_cache = cache or _FRAME_CACHE
    cached = target_cache.get(key)
    if cached is not None:
        return cached

    result = _enrich_with_ws_data(
        _prepare_frame(frame),
        symbol,
        ws_manager if interval == "15m" else None,
        fallback_book=fallback_book if interval == "15m" else None,
    )
    for warning in _sanity_check_prepared_frame(result, symbol, interval):
        LOG.warning(
            "prepared frame quality defect | symbol=%s interval=%s defect=%s",
            symbol,
            interval,
            warning,
        )
    target_cache.put(key, result)
    return result


def _ws_enrichment_signature(
    symbol: str,
    ws_manager: Any | None,
) -> tuple[object, ...] | None:
    if ws_manager is None or not symbol:
        return None

    book = getattr(ws_manager, "_book", {}).get(symbol)
    qty = getattr(ws_manager, "_book_qty", {}).get(symbol)
    snapshot = None
    getter = getattr(ws_manager, "get_agg_trade_snapshot", None)
    if callable(getter):
        try:
            snapshot = getter(symbol)
        except DEFENSIVE_EXC:
            snapshot = None

    def _rounded(value: object) -> float | None:
        numeric = _as_optional_float(value)
        return None if numeric is None else round(numeric, 8)

    signature: list[object] = []
    if isinstance(book, tuple):
        signature.extend(_rounded(item) for item in book[:2])
    else:
        signature.extend((None, None))
    if isinstance(qty, tuple):
        signature.extend(_rounded(item) for item in qty[:2])
    else:
        signature.extend((None, None))
    if snapshot is not None:
        signature.extend(
            (
                int(getattr(snapshot, "trade_count", 0) or 0),
                _rounded(getattr(snapshot, "buy_qty", None)),
                _rounded(getattr(snapshot, "sell_qty", None)),
                _rounded(getattr(snapshot, "delta_ratio", None)),
            )
        )
    else:
        signature.extend((0, None, None, None))
    rollups_fn = getattr(ws_manager, "get_liquidation_rollups", None) if ws_manager else None
    if callable(rollups_fn):
        try:
            rollups = rollups_fn(symbol, window_seconds=900)
        except DEFENSIVE_EXC:
            rollups = None
        if isinstance(rollups, dict):
            signature.extend(
                (
                    _rounded(rollups.get("liquidation_long_notional")),
                    _rounded(rollups.get("liquidation_short_notional")),
                    _rounded(rollups.get("liquidation_score")),
                )
            )
        else:
            signature.extend((None, None, None))
    else:
        signature.extend((None, None, None))
    return tuple(signature)


def _enrich_with_ws_data(
    work: pl.DataFrame,
    symbol: str,
    ws_manager: Any | None,
    fallback_book: tuple[float | None, float | None, float | None, float | None] | None = None,
) -> pl.DataFrame:
    """Merge current WebSocket bookTicker and aggTrade context into work_15m."""
    if work.is_empty() or (ws_manager is None and fallback_book is None):
        return work

    book = getattr(ws_manager, "_book", {}).get(symbol, (None, None)) if ws_manager else None
    qty = getattr(ws_manager, "_book_qty", {}).get(symbol, (None, None)) if ws_manager else None
    if (not isinstance(book, tuple) or book == (None, None)) and fallback_book is not None:
        book = fallback_book[:2]
    if (not isinstance(qty, tuple) or qty == (None, None)) and fallback_book is not None:
        qty = fallback_book[2:4]
    bid_price, ask_price = book if isinstance(book, tuple) else (None, None)
    bid_qty, ask_qty = qty if isinstance(qty, tuple) else (None, None)
    if fallback_book is not None:
        fb_bid, fb_ask, fb_bid_qty, fb_ask_qty = fallback_book
        if bid_price is None:
            bid_price = fb_bid
        if ask_price is None:
            ask_price = fb_ask
        if bid_qty is None:
            bid_qty = fb_bid_qty
        if ask_qty is None:
            ask_qty = fb_ask_qty
    work = work.with_columns(
        [
            pl.lit(_as_optional_float(bid_price)).cast(pl.Float64).alias("bid_price"),
            pl.lit(_as_optional_float(ask_price)).cast(pl.Float64).alias("ask_price"),
            pl.lit(_as_optional_float(bid_qty)).cast(pl.Float64).alias("bid_qty"),
            pl.lit(_as_optional_float(ask_qty)).cast(pl.Float64).alias("ask_qty"),
        ]
    )

    snapshot = None
    getter = getattr(ws_manager, "get_agg_trade_snapshot", None) if ws_manager else None
    if callable(getter):
        try:
            snapshot = getter(symbol)
        except DEFENSIVE_EXC:
            snapshot = None
    if snapshot is not None:
        buy_qty = _as_optional_float(getattr(snapshot, "buy_qty", None)) or 0.0
        sell_qty = _as_optional_float(getattr(snapshot, "sell_qty", None)) or 0.0
        total = buy_qty + sell_qty
        if total > 0.0:
            buy_share = buy_qty / total
            signed_flow = _as_optional_float(getattr(snapshot, "delta_ratio", None))
            if signed_flow is None:
                signed_flow = (buy_share - 0.5) * 2.0
            row_index = "__ws_row_nr"
            base_delta_expr = (
                pl.col("delta_ratio") if "delta_ratio" in work.columns else pl.lit(0.5)
            )
            work = work.with_row_index(row_index)
            work = work.with_columns(
                [
                    pl.when(pl.col(row_index) == (work.height - 1))
                    .then(pl.lit(buy_share))
                    .otherwise(base_delta_expr)
                    .cast(pl.Float64)
                    .alias("delta_ratio"),
                    pl.lit(buy_share).cast(pl.Float64).alias("live_delta_ratio"),
                    pl.lit(signed_flow).cast(pl.Float64).alias("signed_order_flow"),
                ]
            ).drop(row_index)

    if ws_manager is not None:
        rollups_fn = getattr(ws_manager, "get_liquidation_rollups", None)
        if callable(rollups_fn):
            try:
                rollups = rollups_fn(symbol, window_seconds=900)
            except DEFENSIVE_EXC:
                rollups = None
            if isinstance(rollups, dict):
                liq_columns: list[pl.Expr] = []
                long_notional = _as_optional_float(rollups.get("liquidation_long_notional"))
                short_notional = _as_optional_float(rollups.get("liquidation_short_notional"))
                liq_score = _as_optional_float(rollups.get("liquidation_score"))
                if long_notional is not None:
                    liq_columns.append(
                        pl.lit(long_notional).cast(pl.Float64).alias("liquidation_long_notional")
                    )
                if short_notional is not None:
                    liq_columns.append(
                        pl.lit(short_notional).cast(pl.Float64).alias("liquidation_short_notional")
                    )
                if liq_score is not None:
                    liq_columns.append(
                        pl.lit(liq_score).cast(pl.Float64).alias("liquidation_score")
                    )
                if liq_columns:
                    work = work.with_columns(liq_columns)

    work = add_microstructure_features(work)
    work = add_session_cvd(work)
    return work.with_columns(
        [
            pl.col("signed_order_flow").fill_null(0.0).fill_nan(0.0),
            pl.col("session_cvd").fill_null(0.0).fill_nan(0.0),
            pl.col("tob_imbalance").fill_null(0.0).fill_nan(0.0),
            pl.col("microprice_deviation_pct").fill_null(0.0).fill_nan(0.0),
        ]
    )


def _to_polars(df: object) -> pl.DataFrame:
    """Normalize supported frame-like values to Polars."""
    if isinstance(df, pl.DataFrame):
        return df
    if type(df).__module__.startswith("pandas"):
        msg = "prepare_symbol expects Polars frames; pandas inputs are unsupported"
        raise TypeError(msg)
    return pl.DataFrame(cast("Any", df))


def prepare_symbol(
    universe_symbol: UniverseSymbol,
    frames: SymbolFrames,
    *,
    minimums: dict[str, int] | None = None,
    settings: Any | None = None,
    ws_manager: Any | None = None,
) -> PreparedSymbol | None:
    """Prepare a symbol for signal detection by computing all indicators.

    Returns None if there is insufficient historical data.
    """
    _log = logging.getLogger("bot.features")

    sym = universe_symbol.symbol

    minimums = minimums or min_required_bars()
    len_4h = frames.df_4h.height if frames.df_4h is not None else 0
    len_1h = frames.df_1h.height
    len_15m = frames.df_15m.height
    len_5m = frames.df_5m.height if frames.df_5m is not None else 0

    required_timeframes = ("5m", "15m", "1h", "4h")
    if not has_minimum_bars(
        frames,
        minimums=minimums,
        required_timeframes=required_timeframes,
    ):
        _log.info(
            "%s: insufficient frame data | 1h=%d/%d 15m=%d/%d 5m=%d/%d 4h=%d/%d",
            sym,
            len_1h,
            minimums["1h"],
            len_15m,
            minimums["15m"],
            len_5m,
            minimums["5m"],
            len_4h,
            minimums["4h"],
        )
        return None

    work_1h = _cached_prepare_frame(_to_polars(frames.df_1h), symbol=sym, interval="1h")
    data_quality_flags = take_frame_indicator_fallbacks()
    fallback_book = (frames.bid_price, frames.ask_price, frames.bid_qty, frames.ask_qty)
    work_15m = _cached_prepare_frame(
        _to_polars(frames.df_15m),
        symbol=sym,
        interval="15m",
        ws_manager=ws_manager,
        fallback_book=fallback_book,
    )
    data_quality_flags.extend(take_frame_indicator_fallbacks())
    if ws_manager is None and (frames.bid_qty is not None or frames.ask_qty is not None):
        work_15m = _enrich_with_ws_data(
            work_15m,
            sym,
            None,
            fallback_book=fallback_book,
        )
    work_5m = None
    if frames.df_5m is not None and not frames.df_5m.is_empty():
        work_5m = _cached_prepare_frame(_to_polars(frames.df_5m), symbol=sym, interval="5m")
        data_quality_flags.extend(take_frame_indicator_fallbacks())
    work_4h = None
    if frames.df_4h is not None and not frames.df_4h.is_empty():
        work_4h = _cached_prepare_frame(_to_polars(frames.df_4h), symbol=sym, interval="4h")
        data_quality_flags.extend(take_frame_indicator_fallbacks())

    prepared_frames: list[tuple[str, pl.DataFrame | None]] = [
        ("1h", work_1h),
        ("15m", work_15m),
    ]
    if work_5m is not None:
        prepared_frames.append(("5m", work_5m))
    if work_4h is not None:
        prepared_frames.append(("4h", work_4h))
    for interval, frame in prepared_frames:
        if frame is None:
            _log.warning("%s: prepare rejected | interval=%s frame=None", sym, interval)
            return None
        defects = _sanity_check_prepared_frame(frame, sym, interval)
        if defects:
            _log.warning(
                "%s: prepare rejected - frame quality defects | interval=%s defects=%s",
                sym,
                interval,
                defects,
            )
            return None

    work_len_1h = len(work_1h) if work_1h is not None else 0
    work_len_15m = len(work_15m) if work_15m is not None else 0
    work_len_5m = len(work_5m) if work_5m is not None else 0
    work_len_4h = len(work_4h) if work_4h is not None else 0

    if min(work_len_1h, work_len_15m) < 30:
        _log.info(
            (
                "%s: insufficient processed data | work_1h=%d work_15m=%d "
                "optional_5m=%d optional_4h=%d need=30"
            ),
            sym,
            work_len_1h,
            work_len_15m,
            work_len_5m,
            work_len_4h,
        )
        return None

    configured_primary = configured_primary_timeframe(settings, sym)
    context_timeframes = configured_context_timeframes(settings, sym)
    primary_timeframe = configured_primary
    primary_work = work_15m
    if configured_primary == "1h":
        primary_work = work_1h
    elif configured_primary == "4h" and work_4h is not None and work_len_4h >= 30:
        primary_work = work_4h
    elif configured_primary == "5m" and work_5m is not None and work_len_5m >= 30:
        primary_work = work_5m
    elif configured_primary != "15m":
        primary_timeframe = "15m"
        _log.info(
            "%s: primary timeframe fallback | requested=%s fallback=15m work_5m=%d work_4h=%d",
            sym,
            configured_primary,
            work_len_5m,
            work_len_4h,
        )

    _log.info(
        (
            "%s: prepared symbol successfully | primary_timeframe=%s work_primary=%d "
            "work_15m=%d work_1h=%d work_5m=%d optional_4h=%d"
        ),
        sym,
        primary_timeframe,
        len(primary_work) if primary_work is not None else 0,
        work_len_15m,
        work_len_1h,
        work_len_5m,
        work_len_4h,
    )

    # Calculate spread and orderbook metrics (prefer REST frames, fall back to enriched 15m)
    def _frame_last(col: str) -> float | None:
        if work_15m is None or work_15m.is_empty() or col not in work_15m.columns:
            return None
        try:
            value = float(work_15m[col][-1])
        except (TypeError, ValueError):
            return None
        return value if math.isfinite(value) else None

    bid_price = frames.bid_price if frames.bid_price is not None else _frame_last("bid_price")
    ask_price = frames.ask_price if frames.ask_price is not None else _frame_last("ask_price")
    bid_qty = frames.bid_qty if frames.bid_qty is not None else _frame_last("bid_qty")
    ask_qty = frames.ask_qty if frames.ask_qty is not None else _frame_last("ask_qty")

    spread_bps = None
    if bid_price is not None and ask_price is not None and bid_price > 0 and ask_price > 0:
        midpoint = (bid_price + ask_price) / 2.0
        if midpoint > 0:
            spread_bps = ((ask_price - bid_price) / midpoint) * 10_000.0
    book_depth_imbalance = depth_imbalance_from_book(
        bid_qty=bid_qty,
        ask_qty=ask_qty,
        delta_ratio=None,
    )
    book_microprice_bias = microprice_bias_from_book(
        bid=bid_price,
        ask=ask_price,
        bid_qty=bid_qty,
        ask_qty=ask_qty,
        delta_ratio=None,
    )

    liquidation_score = _frame_last("liquidation_score")

    work_4h_frame = work_4h if work_4h is not None else pl.DataFrame()
    regime = _market_regime(work_4h_frame, work_1h=work_1h, work_15m=work_15m)

    return PreparedSymbol(
        universe=universe_symbol,
        work_1h=work_1h,
        work_15m=work_15m,
        bid_price=bid_price,
        ask_price=ask_price,
        spread_bps=spread_bps,
        work_5m=work_5m,
        work_4h=work_4h,
        work_primary=primary_work,
        bias_4h=_bias_4h(work_4h_frame),
        bias_1h=_bias_1h(work_1h),  # 1H context for 15M signals
        market_regime=regime,
        structure_1h=_market_structure_1h(work_1h),
        regime_4h_confirmed=_regime_4h_confirmed(work_4h_frame),
        regime_1h_confirmed=_regime_1h_confirmed(work_1h),  # 1H context for 15M signals
        poc_1h=_volume_poc(work_1h, lookback=48),
        poc_15m=_volume_poc(work_15m, lookback=96),
        depth_imbalance=book_depth_imbalance,
        microprice_bias=book_microprice_bias,
        depth_imbalance_source="rest_book_l1" if book_depth_imbalance is not None else None,
        microprice_bias_source="rest_book_l1" if book_microprice_bias is not None else None,
        liquidation_score=liquidation_score,
        liquidation_score_source="force_order" if liquidation_score is not None else None,
        primary_timeframe=primary_timeframe,
        context_timeframes=context_timeframes,
        settings=settings,
        data_quality_flags=sorted(set(data_quality_flags)),
    )


__all__ = [
    "PreparedSymbol",
    "_add_advanced_indicators",
    "_cached_prepare_frame",
    "_prepare_frame",
    "_swing_points",
    "cache_stats",
    "min_required_bars",
    "prepare_symbol",
]
