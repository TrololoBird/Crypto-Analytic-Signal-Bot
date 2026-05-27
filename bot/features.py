"""Technical analysis feature preparation (Polars-native runtime path).

Indicators stay Polars-native and prefer the installed `polars_ta` expression
backend where its semantics match the runtime contract. Pure-Polars formulas are
kept as deterministic fallbacks and for indicators that need project-specific
normalization.

Key indicators (actually used by strategies):
  - Core: ema20/50/200, rsi14, adx14, atr14, macd_*, donchian_*, vwap
  - Advanced: supertrend_dir, bb_pct_b, bb_width, kc_upper/lower/width

Other columns exist for backward compatibility but return neutral values.
"""

from __future__ import annotations

import importlib
from importlib import util as importlib_util
from collections import OrderedDict
from datetime import date, datetime, timezone
import math
import threading
from typing import Any, Iterable, cast

import numpy as np
import polars as pl
import structlog

from . import features_advanced as _features_advanced_module
from . import features_core as _features_core_module
from . import features_oscillators as _features_oscillators_module
from .domain.schemas import PreparedSymbol, SymbolFrames, UniverseSymbol
from .runtime_policy import (
    configured_context_timeframes,
    configured_primary_timeframe,
)
from .features_microstructure import add_microstructure_features
from .features_shared import supertrend_series, wilder_mean
from .features_structure import (
    hull_moving_average as _hull_moving_average_external,
    ichimoku_lines as _ichimoku_lines_external,
    weighted_moving_average as _weighted_moving_average_external,
)
from .websocket.enrichment import depth_imbalance_from_book, microprice_bias_from_book

# Optional polars_ta import. TA-Lib itself is deliberately not imported:
# Windows/Python 3.13 deployments are brittle with that native dependency,
# while the pure-Polars fallbacks below are stable and deterministic.
try:
    _plta_module = importlib_util.find_spec("polars_ta.ta")
except (ImportError, ModuleNotFoundError):
    _plta_module = None

if _plta_module is not None:
    plta = cast(Any, importlib.import_module("polars_ta.ta"))
    _HAS_POLARS_TA = True
else:
    plta = cast(Any, None)
    _HAS_POLARS_TA = False
_USE_POLARS_TA_BACKEND = _HAS_POLARS_TA

try:
    _polars_ols_module = importlib_util.find_spec("polars_ols")
except (ImportError, ModuleNotFoundError):
    _polars_ols_module = None

if _polars_ols_module is not None:
    _polars_ols = cast(Any, importlib.import_module("polars_ols"))
    _polars_ols_ls = cast(Any, importlib.import_module("polars_ols.least_squares"))
    _HAS_POLARS_OLS = True
else:
    _polars_ols = cast(Any, None)
    _polars_ols_ls = cast(Any, None)
    _HAS_POLARS_OLS = False

# Compatibility name for the decomposed feature modules/tests. This tracks the
# native TA-Lib path, which the project deliberately disables on Windows/Python
# 3.13; optional `polars_ta` availability is tracked separately above.
_HAS_TALIB = False

CORE_API = {
    "add_core_features": _features_core_module.add_core_features,
    "adx": _features_core_module.adx,
    "atr": _features_core_module.atr,
    "ema": _features_core_module.ema,
    "realized_volatility": _features_core_module.realized_volatility,
    "roc": _features_core_module.roc,
    "rsi": _features_core_module.rsi,
    "safe_close_position": _features_core_module.safe_close_position,
    "vwap": _features_core_module.vwap,
}
ADVANCED_API = {
    "add_advanced_indicators": _features_advanced_module.add_advanced_indicators,
    "supertrend": _features_advanced_module.supertrend,
}
OSCILLATORS_API = {
    "add_oscillator_features": _features_oscillators_module.add_oscillator_features,
    "cci": _features_oscillators_module.cci,
    "cmf": _features_oscillators_module.cmf,
    "mfi": _features_oscillators_module.mfi,
    "stochastic": _features_oscillators_module.stochastic,
    "ultimate_oscillator": _features_oscillators_module.ultimate_oscillator,
}

LOG = structlog.get_logger("bot.features")
_ADVANCED_FALLBACKS_LOGGED: set[str] = set()

# ---------------------------------------------------------------------------
# Frame-level indicator cache — LRU with unique frame-window keys.
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


class _FrameCache:
    """Best-effort LRU cache for prepared frames.

    Keys include symbol, interval, row count, first/last close time, and the
    latest OHLCV-like values. `_prepare_frame` depends on the whole history
    window, and live partial kline updates keep the same close time while the
    current candle values change.

    Cache access never waits for a contended lock. Missing a cache hit is
    cheaper than blocking the async analysis loop behind another frame update.
    """

    __slots__ = ("_store", "_max_size", "_lock", "_hits", "_misses")

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


def _clean_non_finite(series: pl.Series, *, fill: float) -> pl.Series:
    """Replace NaN/inf/null values with a stable fill value."""
    return series.replace([float("inf"), float("-inf")], None).fill_nan(fill).fill_null(fill)


def _timestamp_ns(value: object) -> int:
    if isinstance(value, str):
        from datetime import datetime

        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if hasattr(value, "timestamp"):
        return int(value.timestamp() * 1e9)
    return int(cast(Any, value))


def _tail_value_signature(row: dict[str, object]) -> tuple[_FrameCacheValue, ...]:
    values: list[_FrameCacheValue] = []
    for column in _FRAME_CACHE_TAIL_COLUMNS:
        raw = row.get(column)
        if raw is None:
            values.append(None)
            continue
        try:
            value = float(cast(Any, raw))
        except (TypeError, ValueError):
            values.append(None)
            continue
        values.append(None if value != value else value)
    return tuple(values)


def _log_indicator_fallback(indicator: str, exc: Exception) -> None:
    if indicator in _ADVANCED_FALLBACKS_LOGGED:
        LOG.debug("advanced indicator fallback reused", indicator=indicator, error=str(exc))
        return
    _ADVANCED_FALLBACKS_LOGGED.add(indicator)
    LOG.info("advanced indicator fallback activated", indicator=indicator, error=str(exc))


def _materialize_series(
    value: pl.Series | pl.Expr | int | float,
    *,
    df: pl.DataFrame,
    name: str,
) -> pl.Series:
    if isinstance(value, pl.Series):
        return value.rename(name)
    if isinstance(value, pl.Expr):
        return df.select(value.alias(name)).to_series()
    return pl.Series(name, [value] * df.height, dtype=pl.Float64)


def _normalize_rsi_scale(series: pl.Series, *, name: str) -> pl.Series:
    """Normalize RSI to the project contract: 0..100."""
    numeric = series.cast(pl.Float64, strict=False)
    max_value = numeric.max()
    min_value = numeric.min()
    try:
        max_float = float(max_value) if max_value is not None else 100.0
        min_float = float(min_value) if min_value is not None else 0.0
    except (TypeError, ValueError):
        return numeric.rename(name)
    if max_float <= 1.5 and min_float >= -0.01:
        return (numeric * 100.0).rename(name)
    return numeric.rename(name)


def _numeric_item(df: pl.DataFrame, row: int, column: str, default: float = 0.0) -> float:
    try:
        value = df.item(row, column)
    except (IndexError, ValueError):
        return default
    try:
        return default if value is None else float(value)
    except (TypeError, ValueError):
        return default


def _as_float_like(value: object, default: float = 0.0) -> float:
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    return default


def _as_optional_float(value: object) -> float | None:
    try:
        numeric = float(cast(Any, value)) if value is not None else None
    except (TypeError, ValueError):
        return None
    if numeric is None or not np.isfinite(numeric):
        return None
    return numeric


def _finite_float(value: object, default: float = 0.0) -> float:
    numeric = _as_optional_float(value)
    return default if numeric is None else numeric


def min_required_bars(
    *,
    min_bars_15m: int = 210,
    min_bars_1h: int = 210,
    min_bars_5m: int = 96,
    min_bars_4h: int = 210,
) -> dict[str, int]:
    return {
        "15m": int(min_bars_15m),
        "1h": int(min_bars_1h),
        "5m": int(min_bars_5m),
        "4h": int(min_bars_4h),
    }


def has_minimum_bars(
    frames: SymbolFrames,
    *,
    minimums: dict[str, int],
    required_timeframes: Iterable[str] | None = None,
) -> bool:
    required = set(required_timeframes) if required_timeframes is not None else set(minimums)
    frame_by_timeframe = {
        "5m": frames.df_5m,
        "15m": frames.df_15m,
        "1h": frames.df_1h,
        "4h": frames.df_4h,
    }
    for timeframe, frame in frame_by_timeframe.items():
        if timeframe not in required:
            continue
        required_bars = int(minimums.get(timeframe, 0) or 0)
        if required_bars <= 0:
            continue
        available = 0 if frame is None else frame.height
        if available < required_bars:
            return False
    return True


# ---------------------------------------------------------------------------
# Core indicators using Polars (hand-rolled for exact backward compatibility)
# ---------------------------------------------------------------------------


def _ema(df: pl.DataFrame, period: int) -> pl.Series:
    """Exponential Moving Average using polars_ta or pure Polars."""
    if _USE_POLARS_TA_BACKEND and _HAS_POLARS_TA and hasattr(plta, "EMA"):
        try:
            return _materialize_series(
                plta.EMA(pl.col("close"), timeperiod=int(period)),
                df=df,
                name=f"ema{period}",
            )
        except Exception as exc:
            _log_indicator_fallback("ema_polars_ta", exc)
    return _materialize_series(
        df["close"].ewm_mean(span=period, adjust=False), df=df, name=f"ema{period}"
    )


def _rsi(df: pl.DataFrame, period: int = 14) -> pl.Series:
    """Wilder's RSI using the project-verified RMA seed semantics."""
    close = df["close"].cast(pl.Float64, strict=False)
    delta = close.diff()
    gains = delta.clip(lower_bound=0.0)
    losses = (-delta).clip(lower_bound=0.0)

    avg_gain = _materialize_series(
        wilder_mean(
            _materialize_series(gains, df=df, name="gain"),
            period=period,
            name="avg_gain",
            seed_offset=1,
        ),
        df=df,
        name="avg_gain",
    )
    avg_loss = _materialize_series(
        wilder_mean(
            _materialize_series(losses, df=df, name="loss"),
            period=period,
            name="avg_loss",
            seed_offset=1,
        ),
        df=df,
        name="avg_loss",
    )

    rs = avg_gain / avg_loss
    rsi_raw = (100.0 - (100.0 / (1.0 + rs))).fill_nan(50.0)
    return _materialize_series(
        pl.when((avg_loss == 0.0) & (avg_gain > 0.0))
        .then(100.0)
        .when((avg_gain == 0.0) & (avg_loss > 0.0))
        .then(0.0)
        .when((avg_gain == 0.0) & (avg_loss == 0.0))
        .then(50.0)
        .otherwise(rsi_raw),
        df=df,
        name=f"rsi{period}",
    )


def _atr(df: pl.DataFrame, period: int = 14) -> pl.Series:
    """Average True Range using Wilder smoothing with an SMA seed."""
    high = df["high"].cast(pl.Float64, strict=False)
    low = df["low"].cast(pl.Float64, strict=False)
    close = df["close"].cast(pl.Float64, strict=False)
    prev_close = close.shift(1)

    tr = pl.max_horizontal(
        (high - low).abs(),
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    )

    tr_series = _materialize_series(tr, df=df, name="true_range")
    return _materialize_series(
        wilder_mean(tr_series, period=period, name=f"atr{period}"),
        df=df,
        name=f"atr{period}",
    )


def _adx(df: pl.DataFrame, period: int = 14) -> pl.Series:
    """Average Directional Index."""
    # Pure Polars ADX avoids the native TA-Lib dependency.
    high = df["high"]
    low = df["low"]

    up_move = high.diff()
    down_move = -low.diff()

    plus_dm = _materialize_series(
        pl.when((up_move > down_move) & (up_move > 0.0)).then(up_move).otherwise(0.0),
        df=df,
        name="plus_dm",
    )
    minus_dm = _materialize_series(
        pl.when((down_move > up_move) & (down_move > 0.0)).then(down_move).otherwise(0.0),
        df=df,
        name="minus_dm",
    )

    atr = _atr(df, period)
    atr_safe = _clean_non_finite(atr, fill=1e-9).replace(0.0, 1e-9)
    plus_di = 100.0 * wilder_mean(plus_dm, period=period, name="plus_dm_smoothed") / atr_safe
    minus_di = 100.0 * wilder_mean(minus_dm, period=period, name="minus_dm_smoothed") / atr_safe

    di_sum = (plus_di + minus_di).replace(0.0, None)
    dx = _clean_non_finite(100.0 * (plus_di - minus_di).abs() / di_sum, fill=0.0)
    return _materialize_series(
        _clean_non_finite(
            wilder_mean(dx, period=period, name=f"adx{period}", seed_offset=period - 1),
            fill=0.0,
        ).clip(0.0, 100.0),
        df=df,
        name=f"adx{period}",
    )


def _vwap_session_key(value: object) -> date | None:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).date() if value.tzinfo else value.date()
    if isinstance(value, date):
        return value
    return None


def _is_temporal_dtype(dtype: pl.DataType | None) -> bool:
    return bool(dtype is not None and getattr(dtype, "is_temporal", lambda: False)())


def _infer_epoch_time_unit(values: pl.Series) -> str | None:
    values = values.drop_nulls()
    if values.is_empty():
        return None
    try:
        max_abs = float(values.abs().max())
    except (TypeError, ValueError):
        return None
    if max_abs >= 1_000_000_000_000_000_000:
        return "ns"
    if max_abs >= 1_000_000_000_000_000:
        return "us"
    if max_abs >= 100_000_000_000:
        return "ms"
    return "s"


def _coerce_temporal_columns(df: pl.DataFrame) -> pl.DataFrame:
    conversions: list[pl.Expr] = []
    for column in ("time", "open_time", "close_time"):
        dtype = df.schema.get(column)
        if dtype is None or _is_temporal_dtype(dtype):
            continue
        if (
            getattr(dtype, "is_integer", lambda: False)()
            or getattr(dtype, "is_float", lambda: False)()
        ):
            unit = _infer_epoch_time_unit(df[column])
            if unit is not None:
                conversions.append(
                    pl.from_epoch(pl.col(column).cast(pl.Int64), time_unit=unit)
                    .dt.replace_time_zone("UTC")
                    .alias(column)
                )
        elif dtype == pl.String:
            conversions.append(
                pl.col(column).str.to_datetime(strict=False, time_zone="UTC").alias(column)
            )
    if not conversions:
        return df
    return df.with_columns(conversions)


def _vwap(df: pl.DataFrame) -> pl.Series:
    """Volume Weighted Average Price, reset per UTC session when timestamps exist."""
    typical_price = (df["high"] + df["low"] + df["close"]) / 3.0
    pv = typical_price * df["volume"]

    time_column = next(
        (column for column in ("close_time", "time", "open_time") if column in df.columns),
        None,
    )
    if time_column is not None and _is_temporal_dtype(df.schema.get(time_column)):
        # Create a session key (date) for reset
        session_key = df[time_column].dt.date().alias("_vwap_session")
        temp_df = df.with_columns([pv.alias("_pv"), session_key])

        vwap_expr = (
            pl.col("_pv").cum_sum().over("_vwap_session")
            / pl.col("volume").cum_sum().over("_vwap_session")
        ).forward_fill()

        return _materialize_series(vwap_expr, df=temp_df, name="vwap")

    cpv: pl.Series = pv.cum_sum()
    cv: pl.Series = df["volume"].cum_sum()

    vwap = (cpv / cv).forward_fill()
    return _materialize_series(vwap, df=df, name="vwap")


def _roc(df: pl.DataFrame, period: int = 10) -> pl.Series:
    if _USE_POLARS_TA_BACKEND and _HAS_POLARS_TA and hasattr(plta, "ROC"):
        try:
            return _materialize_series(
                plta.ROC(pl.col("close"), timeperiod=int(period)),
                df=df,
                name=f"roc{period}",
            )
        except Exception as exc:
            _log_indicator_fallback("roc_polars_ta", exc)
    prev_close = df["close"].shift(period)
    return (((df["close"] / prev_close) - 1.0) * 100.0).fill_nan(0.0).rename(f"roc{period}")


def _stochastic(
    df: pl.DataFrame,
    period: int = 14,
    smooth_k: int = 3,
    smooth_d: int = 3,
) -> tuple[pl.Series, pl.Series]:
    rolling_low = df["low"].rolling_min(window_size=period)
    rolling_high = df["high"].rolling_max(window_size=period)
    width = rolling_high - rolling_low
    raw_k = _clean_non_finite(((df["close"] - rolling_low) / width) * 100.0, fill=50.0)
    k = _clean_non_finite(raw_k.rolling_mean(window_size=smooth_k), fill=50.0).rename("stoch_k14")
    d = _clean_non_finite(k.rolling_mean(window_size=smooth_d), fill=50.0).rename("stoch_d14")
    return k, d


def _cci(df: pl.DataFrame, period: int = 20) -> pl.Series:
    typical_price = (df["high"] + df["low"] + df["close"]) / 3.0
    sma = typical_price.rolling_mean(window_size=period)
    mean_dev = (typical_price - sma).abs().rolling_mean(window_size=period)
    return _clean_non_finite((typical_price - sma) / (0.015 * mean_dev), fill=0.0).rename(
        f"cci{period}"
    )


def _mfi(df: pl.DataFrame, period: int = 14) -> pl.Series:
    typical_price = (df["high"] + df["low"] + df["close"]) / 3.0
    money_flow = typical_price * df["volume"]
    delta = typical_price.diff()

    pos_flow = pl.when(delta > 0.0).then(money_flow).otherwise(0.0)
    neg_flow = pl.when(delta < 0.0).then(money_flow).otherwise(0.0)

    pos_sum = pos_flow.rolling_sum(window_size=period)
    neg_sum = neg_flow.rolling_sum(window_size=period)

    mfi_raw = 100.0 - (100.0 / (1.0 + (pos_sum / neg_sum)))

    return _materialize_series(
        pl.when((neg_sum <= 0.0) & (pos_sum <= 0.0))
        .then(50.0)
        .when(neg_sum <= 0.0)
        .then(100.0)
        .otherwise(mfi_raw),
        df=df,
        name=f"mfi{period}",
    )


def _cmf(df: pl.DataFrame, period: int = 20) -> pl.Series:
    width = df["high"] - df["low"]
    mfm = (
        pl.when(width > 0.0)
        .then(((df["close"] - df["low"]) - (df["high"] - df["close"])) / width)
        .otherwise(0.0)
    )

    money_flow_volume = mfm * df["volume"]
    volume_sum = df["volume"].rolling_sum(window_size=period)

    return _materialize_series(
        (money_flow_volume.rolling_sum(window_size=period) / volume_sum).fill_nan(0.0),
        df=df,
        name=f"cmf{period}",
    )


def _ultimate_oscillator(df: pl.DataFrame, p1: int = 7, p2: int = 14, p3: int = 28) -> pl.Series:
    prev_close = df["close"].shift(1)
    min_low = _materialize_series(
        pl.min_horizontal(df["low"], prev_close), df=df, name="uo_min_low"
    )
    max_high = _materialize_series(
        pl.max_horizontal(df["high"], prev_close), df=df, name="uo_max_high"
    )
    bp = (df["close"] - min_low).rename("uo_bp")
    tr = (max_high - min_low).rename("uo_tr")
    avg1 = bp.rolling_sum(window_size=p1) / tr.rolling_sum(window_size=p1)
    avg2 = bp.rolling_sum(window_size=p2) / tr.rolling_sum(window_size=p2)
    avg3 = bp.rolling_sum(window_size=p3) / tr.rolling_sum(window_size=p3)
    uo = (100.0 * ((4.0 * avg1) + (2.0 * avg2) + avg3) / 7.0).rename("uo")
    return _clean_non_finite(uo, fill=50.0)


def _realized_volatility(df: pl.DataFrame, period: int = 20) -> pl.Series:
    log_returns = df["close"].log() - df["close"].shift(1).log()
    return _materialize_series(
        (log_returns.rolling_std(window_size=period) * float(np.sqrt(period)) * 100.0).fill_nan(
            0.0
        ),
        df=df,
        name=f"realized_vol_{period}",
    )


def _add_session_features(work: pl.DataFrame, period: int = 20) -> pl.DataFrame:
    if "close_time" not in work.columns or not _is_temporal_dtype(work.schema.get("close_time")):
        return work.with_columns(
            [
                pl.lit(0.0).alias("session_asia"),
                pl.lit(0.0).alias("session_london"),
                pl.lit(0.0).alias("session_ny"),
                pl.lit(0.0).alias("session_overlap"),
                pl.lit(0.0).alias("session_asia_vol_20"),
                pl.lit(0.0).alias("session_london_vol_20"),
                pl.lit(0.0).alias("session_ny_vol_20"),
                pl.lit(0.0).alias("session_overlap_vol_20"),
            ]
        )

    hour = pl.col("close_time").dt.hour()
    log_return = pl.col("close").log() - pl.col("close").shift(1).log()
    work = work.with_columns(
        [
            hour.is_between(0, 8, closed="left").cast(pl.Float64).alias("session_asia"),
            hour.is_between(7, 16, closed="left").cast(pl.Float64).alias("session_london"),
            hour.is_between(13, 22, closed="left").cast(pl.Float64).alias("session_ny"),
            hour.is_between(13, 16, closed="left").cast(pl.Float64).alias("session_overlap"),
        ]
    )
    scale = float(np.sqrt(period) * 100.0)
    return work.with_columns(
        [
            (
                pl.when(pl.col("session_asia") == 1.0)
                .then(log_return)
                .otherwise(None)
                .rolling_std(window_size=period)
                * scale
            )
            .fill_null(0.0)
            .fill_nan(0.0)
            .alias("session_asia_vol_20"),
            (
                pl.when(pl.col("session_london") == 1.0)
                .then(log_return)
                .otherwise(None)
                .rolling_std(window_size=period)
                * scale
            )
            .fill_null(0.0)
            .fill_nan(0.0)
            .alias("session_london_vol_20"),
            (
                pl.when(pl.col("session_ny") == 1.0)
                .then(log_return)
                .otherwise(None)
                .rolling_std(window_size=period)
                * scale
            )
            .fill_null(0.0)
            .fill_nan(0.0)
            .alias("session_ny_vol_20"),
            (
                pl.when(pl.col("session_overlap") == 1.0)
                .then(log_return)
                .otherwise(None)
                .rolling_std(window_size=period)
                * scale
            )
            .fill_null(0.0)
            .fill_nan(0.0)
            .alias("session_overlap_vol_20"),
        ]
    )


def _safe_close_position(df: pl.DataFrame, window: int = 20) -> pl.Series:
    """Close position within rolling high-low range (0-1)."""
    rolling_low = df["low"].rolling_min(window_size=window)
    rolling_high = df["high"].rolling_max(window_size=window)
    width = rolling_high - rolling_low

    value = _clean_non_finite((df["close"] - rolling_low) / width, fill=0.5)
    return value.clip(0.0, 1.0).rename("close_position")


def _ichimoku_lines(
    df: pl.DataFrame,
) -> tuple[pl.Series, pl.Series, pl.Series, pl.Series]:
    return _ichimoku_lines_external(df)


# ---------------------------------------------------------------------------
# Advanced indicators via pure Polars implementations.
# ---------------------------------------------------------------------------


def _bollinger_bands(
    close: pl.Series, period: int = 20, nbdev: float = 2.0
) -> tuple[pl.Series, pl.Series, pl.Series]:
    """Bollinger Bands - pure Polars implementation.

    Returns (upper, middle, lower) bands.
    """
    middle = close.rolling_mean(window_size=period).rename("bb_middle")
    std = close.rolling_std(window_size=period, ddof=1).rename("bb_std")

    upper = middle + nbdev * std
    lower = middle - nbdev * std

    return upper, middle, lower


def _weighted_moving_average(series: pl.Series, period: int, *, name: str) -> pl.Series:
    return _weighted_moving_average_external(series, period, name=name)


def _hull_moving_average(close: pl.Series, period: int, *, name: str) -> pl.Series:
    return _hull_moving_average_external(close, period, name=name)


def _keltner_channels(
    df: pl.DataFrame,
    period: int = 20,
    multiplier: float = 2.0,
    atr_period: int = 10,
) -> tuple[pl.Series, pl.Series, pl.Series]:
    """Keltner Channels - pure Polars implementation using ATR.

    Returns (upper, middle, lower) channels.
    """
    typical_price = (df["high"] + df["low"] + df["close"]) / 3.0
    middle = typical_price.ewm_mean(span=period, adjust=False).rename("kc_middle")

    # ATR for channel width
    high = df["high"]
    low = df["low"]
    close = df["close"]
    prev_close = close.shift(1)

    tr = pl.max_horizontal(
        (high - low).abs(),
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    )
    tr_series = _materialize_series(tr, df=df, name="true_range")
    atr = _materialize_series(
        wilder_mean(tr_series, period=atr_period, name="kc_atr"),
        df=df,
        name="kc_atr",
    )

    upper = middle + multiplier * atr
    lower = middle - multiplier * atr

    return upper, middle, lower


def _parabolic_sar(
    df: pl.DataFrame,
    *,
    step: float = 0.02,
    max_step: float = 0.2,
) -> tuple[pl.Series, pl.Series, pl.Series]:
    high_vals = [_finite_float(v) for v in df["high"]]
    low_vals = [_finite_float(v) for v in df["low"]]
    close_vals = [_finite_float(v) for v in df["close"]]
    size = len(close_vals)
    if size == 0:
        empty = pl.Series("psar_long", [], dtype=pl.Float64)
        return (
            empty,
            pl.Series("psar_short", [], dtype=pl.Float64),
            pl.Series("psar_reversal", [], dtype=pl.Float64),
        )

    long_psar: list[float | None] = [None] * size
    short_psar: list[float | None] = [None] * size
    reversals: list[float] = [0.0] * size

    is_long = True if size < 2 else close_vals[1] >= close_vals[0]
    af = step
    ep = high_vals[0] if is_long else low_vals[0]
    psar = low_vals[0] if is_long else high_vals[0]

    for i in range(size):
        if i == 0:
            if is_long:
                long_psar[i] = psar
            else:
                short_psar[i] = psar
            continue
        prev_psar = psar
        psar = prev_psar + af * (ep - prev_psar)
        if is_long:
            psar = min(psar, low_vals[i - 1], low_vals[i - 2] if i > 1 else low_vals[i - 1])
            if low_vals[i] < psar:
                is_long = False
                reversals[i] = -1.0
                psar = ep
                ep = low_vals[i]
                af = step
                short_psar[i] = psar
                continue
            if high_vals[i] > ep:
                ep = high_vals[i]
                af = min(af + step, max_step)
            long_psar[i] = psar
        else:
            psar = max(psar, high_vals[i - 1], high_vals[i - 2] if i > 1 else high_vals[i - 1])
            if high_vals[i] > psar:
                is_long = True
                reversals[i] = 1.0
                psar = ep
                ep = high_vals[i]
                af = step
                long_psar[i] = psar
                continue
            if low_vals[i] < ep:
                ep = low_vals[i]
                af = min(af + step, max_step)
            short_psar[i] = psar

    return (
        pl.Series("psar_long", long_psar, dtype=pl.Float64),
        pl.Series("psar_short", short_psar, dtype=pl.Float64),
        pl.Series("psar_reversal", reversals, dtype=pl.Float64),
    )


def _aroon(df: pl.DataFrame, period: int = 14) -> tuple[pl.Series, pl.Series, pl.Series]:
    """Aroon indicator - vectorized via rolling_map."""
    high = df["high"]
    low = df["low"]

    def bars_since_argmax(s: pl.Series) -> int:
        best_idx: int | None = None
        best_value: float | None = None
        for idx, raw in enumerate(s):
            value = _as_optional_float(raw)
            if value is None:
                continue
            if best_value is None or value > best_value:
                best_idx = idx
                best_value = value
        return 0 if best_idx is None else len(s) - 1 - best_idx

    def bars_since_argmin(s: pl.Series) -> int:
        best_idx: int | None = None
        best_value: float | None = None
        for idx, raw in enumerate(s):
            value = _as_optional_float(raw)
            if value is None:
                continue
            if best_value is None or value < best_value:
                best_idx = idx
                best_value = value
        return 0 if best_idx is None else len(s) - 1 - best_idx

    up_days = high.rolling_map(bars_since_argmax, window_size=period + 1)
    down_days = low.rolling_map(bars_since_argmin, window_size=period + 1)

    aroon_up = (period - up_days) / period * 100.0
    aroon_down = (period - down_days) / period * 100.0

    return (
        _materialize_series(aroon_up, df=df, name=f"aroon_up{period}"),
        _materialize_series(aroon_down, df=df, name=f"aroon_down{period}"),
        _materialize_series(aroon_up - aroon_down, df=df, name=f"aroon_osc{period}"),
    )


def _fisher_transform(df: pl.DataFrame, period: int = 10) -> tuple[pl.Series, pl.Series]:
    high = [_finite_float(v) for v in df["high"]]
    low = [_finite_float(v) for v in df["low"]]
    close = [_finite_float(v) for v in df["close"]]
    size = len(close)
    values: list[float] = [0.0] * size
    fisher: list[float] = [0.0] * size
    for i in range(size):
        start = max(0, i - period + 1)
        hh = max(high[start : i + 1])
        ll = min(low[start : i + 1])
        width = max(hh - ll, 1e-9)
        price_norm = (close[i] - ll) / width
        raw = 2.0 * (price_norm - 0.5)
        prev_v = values[i - 1] if i > 0 else 0.0
        v = 0.33 * raw + 0.67 * prev_v
        v = max(min(v, 0.999), -0.999)
        values[i] = v
        prev_f = fisher[i - 1] if i > 0 else 0.0
        fisher[i] = 0.5 * np.log((1.0 + v) / (1.0 - v)) + 0.5 * prev_f
    fisher_series = pl.Series("fisher", fisher, dtype=pl.Float64)
    fisher_signal = fisher_series.ewm_mean(span=5, adjust=False).rename("fisher_signal")
    return fisher_series, fisher_signal


def _squeeze_momentum(
    df: pl.DataFrame, period: int = 20
) -> tuple[pl.Series, pl.Series, pl.Series, pl.Series]:
    bb_upper, bb_mid, bb_lower = _bollinger_bands(df["close"], period=period, nbdev=2.0)
    kc_upper, _, kc_lower = _keltner_channels(df, period=period, multiplier=1.5)
    squeeze_on = (
        ((bb_lower > kc_lower) & (bb_upper < kc_upper)).cast(pl.Float64).rename("squeeze_on")
    )
    squeeze_off = (
        ((bb_lower < kc_lower) & (bb_upper > kc_upper)).cast(pl.Float64).rename("squeeze_off")
    )
    squeeze_no_values = [
        max(0.0, min(1.0, 1.0 - max(_finite_float(on), _finite_float(off))))
        for on, off in zip(squeeze_on, squeeze_off, strict=False)
    ]
    squeeze_no = pl.Series("squeeze_no", squeeze_no_values, dtype=pl.Float64)
    basis = (
        (df["high"].rolling_max(window_size=period) + df["low"].rolling_min(window_size=period))
        / 2.0
        + bb_mid
    ) / 2.0
    hist = _clean_non_finite((df["close"] - basis).ewm_mean(span=5, adjust=False), fill=0.0).rename(
        "squeeze_hist"
    )
    return hist, squeeze_on, squeeze_off, squeeze_no


def _chandelier_exit(
    df: pl.DataFrame, period: int = 22, atr_mult: float = 3.0
) -> tuple[pl.Series, pl.Series, pl.Series]:
    atr = _atr(df, period)
    long_exit = (df["high"].rolling_max(window_size=period) - atr * atr_mult).rename(
        "chandelier_long"
    )
    short_exit = (df["low"].rolling_min(window_size=period) + atr * atr_mult).rename(
        "chandelier_short"
    )
    # Vectorized direction: stay in current trend until opposite stop is hit.
    signals = (
        pl.when(df["close"] > short_exit)
        .then(1.0)
        .when(df["close"] < long_exit)
        .then(-1.0)
        .otherwise(None)
    )
    direction = signals.forward_fill().fill_null(0.0)

    return (
        long_exit,
        short_exit,
        _materialize_series(direction, df=df, name="chandelier_dir"),
    )


def _add_advanced_indicators(df: pl.DataFrame) -> pl.DataFrame:
    """Add advanced technical indicators using pure Polars implementations."""
    result = df

    # --- SuperTrend ---------------------------------------------------------
    st, st_dir = supertrend_series(df, period=10, multiplier=3.0)
    result = result.with_columns(
        [
            st.alias("supertrend"),
            st_dir.alias("supertrend_dir"),
        ]
    )

    # --- OBV ---------------------------------------------------------------
    try:
        if _USE_POLARS_TA_BACKEND and _HAS_POLARS_TA and hasattr(plta, "OBV"):
            obv = _materialize_series(
                plta.OBV(pl.col("close"), pl.col("volume")), df=df, name="obv"
            )
        else:
            close_diff = df["close"].diff()
            direction = _materialize_series(
                pl.when(close_diff > 0.0)
                .then(1.0)
                .when(close_diff < 0.0)
                .then(-1.0)
                .otherwise(0.0),
                df=df,
                name="obv_direction",
            )
            obv = (direction * df["volume"]).cum_sum().rename("obv")
        obv_ema = obv.ewm_mean(span=20, adjust=False)
        result = result.with_columns(
            [
                obv.alias("obv"),
                obv_ema.alias("obv_ema20"),
                (obv > obv_ema).cast(pl.Float64).alias("obv_above_ema"),
            ]
        )
    except Exception as exc:
        _log_indicator_fallback("obv", exc)
        result = result.with_columns(
            [
                pl.lit(0.0).alias("obv"),
                pl.lit(0.0).alias("obv_ema20"),
                pl.lit(0.0).alias("obv_above_ema"),
            ]
        )

    # --- Bollinger Bands - pure Polars implementation ------------------------
    upper, middle, lower = _bollinger_bands(df["close"], period=20, nbdev=2.0)
    bb_pct_b = (df["close"] - lower) / (upper - lower)
    middle_safe = _clean_non_finite(middle.abs(), fill=1e-10).clip(lower_bound=1e-10)
    bb_width = (upper - lower) / middle_safe * 100.0
    result = result.with_columns(
        [
            _clean_non_finite(bb_pct_b, fill=0.5).alias("bb_pct_b"),
            _clean_non_finite(bb_width, fill=0.0).alias("bb_width"),
        ]
    )

    # --- Keltner Channels - pure Polars implementation -----------------------
    kc_upper, kc_middle, kc_lower = _keltner_channels(df, period=20, multiplier=2.0)
    close_safe = _clean_non_finite(df["close"].abs(), fill=1e-10).clip(lower_bound=1e-10)
    kc_width = (kc_upper - kc_lower) / close_safe
    result = result.with_columns(
        [
            kc_upper.alias("kc_upper"),
            kc_lower.alias("kc_lower"),
            _clean_non_finite(kc_width, fill=0.04).alias("kc_width"),
        ]
    )

    # --- HMA (Hull Moving Average) --------------------------------------------
    close = df["close"]
    hma9 = _hull_moving_average(close, 9, name="hma9")
    hma21 = _hull_moving_average(close, 21, name="hma21")
    result = result.with_columns(
        [
            hma9.alias("hma9"),
            hma21.alias("hma21"),
        ]
    )

    # --- PSAR (Parabolic SAR) -------------------------------------------------
    psar_long, psar_short, psar_reversal = _parabolic_sar(df, step=0.02, max_step=0.2)
    result = result.with_columns(
        [
            psar_long.alias("psar_long"),
            psar_short.alias("psar_short"),
            psar_reversal.alias("psar_reversal"),
        ]
    )

    # --- Aroon ---------------------------------------------------------------
    aroon_up, aroon_down, aroon_osc = _aroon(df, period=14)
    result = result.with_columns(
        [
            aroon_up.alias("aroon_up14"),
            aroon_down.alias("aroon_down14"),
            aroon_osc.alias("aroon_osc14"),
        ]
    )

    # --- Stochastic ---------------------------------------------------------
    stoch_k, stoch_d = _stochastic(df, period=14, smooth_k=3, smooth_d=3)
    result = result.with_columns(
        [
            stoch_k.alias("stoch_k14"),
            stoch_d.alias("stoch_d14"),
            (stoch_k - stoch_d).fill_nan(0.0).alias("stoch_h14"),
        ]
    )

    # --- CCI, Williams %R, MFI, CMF, Ultimate Oscillator --------------------
    rolling_high = df["high"].rolling_max(window_size=14)
    rolling_low = df["low"].rolling_min(window_size=14)
    willr = ((rolling_high - df["close"]) / (rolling_high - rolling_low)) * -100.0
    result = result.with_columns(
        [
            _cci(df, 20).fill_nan(0.0).alias("cci20"),
            _clean_non_finite(willr, fill=-50.0).alias("willr14"),
            _mfi(df, 14).fill_nan(50.0).alias("mfi14"),
            _cmf(df, 20).fill_nan(0.0).alias("cmf20"),
            _ultimate_oscillator(df, 7, 14, 28).fill_nan(50.0).alias("uo"),
        ]
    )

    # --- Fisher Transform -----------------------------------------------------
    fisher, fisher_signal = _fisher_transform(df, period=10)
    result = result.with_columns(
        [
            fisher.alias("fisher"),
            fisher_signal.alias("fisher_signal"),
        ]
    )

    # --- Squeeze Momentum ----------------------------------------------------
    squeeze_hist, squeeze_on, squeeze_off, squeeze_no = _squeeze_momentum(df, period=20)
    result = result.with_columns(
        [
            squeeze_hist.alias("squeeze_hist"),
            squeeze_on.alias("squeeze_on"),
            squeeze_off.alias("squeeze_off"),
            squeeze_no.alias("squeeze_no"),
        ]
    )

    # --- Chandelier Exit -----------------------------------------------------
    chandelier_long, chandelier_short, chandelier_dir = _chandelier_exit(
        df, period=22, atr_mult=3.0
    )
    result = result.with_columns(
        [
            chandelier_long.alias("chandelier_long"),
            chandelier_short.alias("chandelier_short"),
            chandelier_dir.alias("chandelier_dir"),
        ]
    )

    result = result.with_columns(
        [
            _volume_profile(result, bins=12),
        ]
    )

    # --- Z-Score and Slope -------------------------------------------------
    zscore30 = (
        (df["close"] - df["close"].rolling_mean(window_size=30))
        / df["close"].rolling_std(window_size=30)
    ).fill_nan(0.0)
    result = result.with_columns(
        [
            _clean_non_finite(zscore30, fill=0.0).alias("zscore30"),
            _roc(df, 5).fill_nan(0.0).alias("slope5"),
        ]
    )

    # --- Ichimoku Cloud - UNUSED by strategies -----------------------------
    tenkan, kijun, senkou_a, senkou_b = _ichimoku_lines(result)
    result = result.with_columns(
        [
            tenkan.alias("ichi_tenkan"),
            kijun.alias("ichi_kijun"),
            senkou_a.alias("ichi_senkou_a"),
            senkou_b.alias("ichi_senkou_b"),
        ]
    )

    return result


def _volume_profile(df: pl.DataFrame, bins: int = 12) -> pl.Expr:
    """Return a scalar point-of-control approximation for the frame."""
    if df.is_empty() or not {"high", "low", "volume"}.issubset(df.columns):
        return pl.lit(None).cast(pl.Float64).alias("volume_profile")

    prices = ((df["high"] + df["low"]) / 2.0).cast(pl.Float64, strict=False)
    volumes = df["volume"].cast(pl.Float64, strict=False)

    # Filter valid prices and volumes
    valid_mask = prices.is_not_null() & prices.is_finite() & volumes.is_not_null() & (volumes > 0.0)
    v_prices = prices.filter(valid_mask)
    v_volumes = volumes.filter(valid_mask)

    if v_prices.is_empty():
        return pl.lit(None).cast(pl.Float64).alias("volume_profile")

    price_min = _as_optional_float(v_prices.min())
    price_max = _as_optional_float(v_prices.max())

    if price_min is None or price_max is None or price_max <= price_min:
        poc = price_max if price_max is not None else price_min
    else:
        bucket_count = max(1, int(bins))
        bucket_size = (price_max - price_min) / bucket_count

        # Vectorized bucketing
        buckets = (
            ((v_prices - price_min) / bucket_size).floor().cast(pl.Int32).clip(0, bucket_count - 1)
        )

        vol_by_bucket = (
            pl.DataFrame({"b": buckets, "v": v_volumes}).group_by("b").agg(pl.col("v").sum())
        )

        if vol_by_bucket.is_empty():
            poc = price_min
        else:
            poc_bucket = int(vol_by_bucket.sort("v", descending=True).row(0)[0])
            poc = price_min + (poc_bucket + 0.5) * bucket_size

    return pl.lit(0.0 if poc is None else poc).cast(pl.Float64).alias("volume_profile")


def _add_polars_ols_features(df: pl.DataFrame) -> pl.DataFrame:
    """Add shared regression-slope features using polars_ols when available."""
    if df.is_empty() or "close" not in df.columns:
        return df
    if not _HAS_POLARS_OLS:
        LOG.debug("polars_ols_unavailable_skipping_ols_features")
        return df
    index_expr = pl.int_range(0, pl.len()).cast(pl.Float64)
    try:
        rolling_kwargs = _polars_ols_ls.RollingKwargs(
            window_size=20,
            min_periods=20,
            use_woodbury=None,
            alpha=None,
            null_policy="drop",
        )
        slope_struct = _polars_ols.compute_rolling_least_squares(
            pl.col("close"),
            index_expr,
            add_intercept=True,
            mode="coefficients",
            rolling_kwargs=rolling_kwargs,
        )
        work = df.with_columns(slope_struct.alias("_ols_close20"))
        work = work.with_columns(
            pl.col("_ols_close20").struct.field("literal").alias("close_ols_slope20")
        )
        return work.drop("_ols_close20").with_columns(
            [
                (pl.col("close_ols_slope20") / pl.col("close") * 100.0)
                .fill_nan(0.0)
                .fill_null(0.0)
                .alias("close_ols_slope_pct20"),
                (
                    pl.col("close_ols_slope20")
                    / pl.when(pl.col("atr14") > 0.0).then(pl.col("atr14")).otherwise(None)
                )
                .fill_nan(0.0)
                .fill_null(0.0)
                .alias("close_ols_slope_atr20"),
            ]
        )
    except Exception as exc:
        _log_indicator_fallback("polars_ols_close_slope20", exc)

    fallback_slope = (pl.col("close") - pl.col("close").shift(19)) / 19.0
    return df.with_columns(fallback_slope.alias("close_ols_slope20")).with_columns(
        [
            (pl.col("close_ols_slope20") / pl.col("close") * 100.0)
            .fill_nan(0.0)
            .fill_null(0.0)
            .alias("close_ols_slope_pct20"),
            (
                pl.col("close_ols_slope20")
                / pl.when(pl.col("atr14") > 0.0).then(pl.col("atr14")).otherwise(None)
            )
            .fill_nan(0.0)
            .fill_null(0.0)
            .alias("close_ols_slope_atr20"),
        ]
    )


# ---------------------------------------------------------------------------
# Main frame preparation
# ---------------------------------------------------------------------------


def _prepare_frame(df: pl.DataFrame) -> pl.DataFrame:
    """Compute all technical indicators for a single OHLCV DataFrame.

    Returns a new DataFrame with NaN-seeded rows dropped.
    All backward-compatible column names are preserved.
    """
    df = _coerce_temporal_columns(df)

    # Core indicators
    work = df.with_columns(
        [
            _ema(df, 20).alias("ema20"),
            _ema(df, 50).alias("ema50"),
            _ema(df, 200).alias("ema200"),
            _rsi(df, 14).alias("rsi14"),
            _adx(df, 14).alias("adx14"),
            _atr(df, 14).alias("atr14"),
        ]
    )

    # MACD
    macd_line = _ema(work, 12) - _ema(work, 26)
    work = work.with_columns(
        [
            macd_line.alias("macd_line"),
        ]
    )

    macd_signal = work["macd_line"].ewm_mean(span=9, adjust=False)
    work = work.with_columns(
        [
            macd_signal.alias("macd_signal"),
            (pl.col("macd_line") - macd_signal).alias("macd_hist"),
        ]
    )

    # Donchian Channels
    work = work.with_columns(
        [
            pl.col("low").rolling_min(window_size=20).alias("donchian_low20"),
            pl.col("high").rolling_max(window_size=20).alias("donchian_high20"),
        ]
    )
    work = work.with_columns(
        [
            pl.col("donchian_low20").shift(1).alias("prev_donchian_low20"),
            pl.col("donchian_high20").shift(1).alias("prev_donchian_high20"),
        ]
    )

    # Volume metrics
    work = work.with_columns(
        [
            pl.col("volume").rolling_mean(window_size=20).alias("volume_mean20"),
        ]
    )
    work = work.with_columns(
        [
            (pl.col("volume") / pl.col("volume_mean20")).alias("volume_ratio20"),
        ]
    )

    # VWAP and bands
    work = work.with_columns(
        [
            _vwap(work).alias("vwap"),
        ]
    )

    price_dev_sq = (work["close"] - work["vwap"]) ** 2
    vwap_time_column = next(
        (
            column
            for column in ("close_time", "time", "open_time")
            if column in work.columns and _is_temporal_dtype(work.schema.get(column))
        ),
        None,
    )
    if vwap_time_column is not None:
        temp_vwap = work.with_columns(
            [
                price_dev_sq.alias("_vwap_dev_sq"),
                pl.col(vwap_time_column).dt.date().alias("_vwap_session"),
            ]
        )
        vwap_std = _materialize_series(
            (
                pl.col("_vwap_dev_sq").cum_sum().over("_vwap_session")
                / pl.col("_vwap_dev_sq").cum_count().over("_vwap_session")
            ).sqrt(),
            df=temp_vwap,
            name="vwap_std",
        )
    elif work.height:
        denom = pl.Series("n", range(1, work.height + 1), dtype=pl.Float64)
        vwap_std = (price_dev_sq.cum_sum() / denom).sqrt()
    else:
        vwap_std = price_dev_sq
    work = work.with_columns(
        [
            vwap_std.alias("vwap_std"),
            (pl.col("vwap") + vwap_std).alias("vwap_upper1"),
            (pl.col("vwap") - vwap_std).alias("vwap_lower1"),
            (pl.col("vwap") + 2.0 * vwap_std).alias("vwap_upper2"),
            (pl.col("vwap") - 2.0 * vwap_std).alias("vwap_lower2"),
            (((pl.col("close") - pl.col("vwap")) / pl.col("vwap")) * 100.0)
            .fill_nan(0.0)
            .alias("vwap_deviation_pct"),
        ]
    )

    # Delta ratio (if taker_buy available)
    if "taker_buy_base_volume" in work.columns:
        work = work.with_columns(
            [
                (
                    (pl.col("taker_buy_base_volume") / pl.col("volume"))
                    .rolling_mean(window_size=5)
                    .clip(0.0, 1.0)
                    .alias("delta_ratio")
                ),
            ]
        )
    else:
        work = work.with_columns([pl.lit(0.5).alias("delta_ratio")])

    # ATR %
    work = work.with_columns(
        [
            ((pl.col("atr14") / pl.col("close")) * 100.0).clip(lower_bound=0.001).alias("atr_pct"),
        ]
    )

    # Close position
    work = work.with_columns(
        [
            _safe_close_position(work, window=20).alias("close_position"),
        ]
    )

    # Advanced indicators
    work = _add_advanced_indicators(work)
    work = add_microstructure_features(work)
    work = _add_polars_ols_features(work)
    work = work.with_columns(
        [
            _roc(work, 10).fill_nan(0.0).alias("roc10"),
            _realized_volatility(work, 20).fill_nan(0.0).alias("realized_vol_20"),
            (
                (
                    pl.col("vwap_deviation_pct")
                    - pl.col("vwap_deviation_pct").rolling_mean(window_size=20)
                )
                / pl.col("vwap_deviation_pct").rolling_std(window_size=20, ddof=1)
            )
            .fill_nan(0.0)
            .alias("vwap_deviation_z20"),
        ]
    )
    work = _add_session_features(work)

    # Drop rows with insufficient data
    # Filter where ema200 or donchian_low20 is null
    work = work.filter(pl.col("ema200").is_not_null() & pl.col("donchian_low20").is_not_null())

    return work


# ---------------------------------------------------------------------------
# 4h bias helper
# ---------------------------------------------------------------------------


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
            swing_high_values[pivot_idx] = pivot_high > max(left_highs) and pivot_high > confirm_high
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
    """Return non-fatal warnings for impossible or suspicious prepared features.

    The function is deliberately observational: it never raises, never mutates
    the frame, and never blocks signal generation. It catches contract drift
    early in telemetry when indicator ranges become impossible after changes to
    Polars, optional feature backends, or exchange payload shape.
    """
    warnings: list[str] = []
    missing = REQUIRED_COLS - set(work.columns)
    if missing:
        warnings.append(f"Missing required columns: {missing}")
    if work.is_empty():
        warnings.append(f"{symbol}/{interval}: prepared frame is empty")
        return warnings

    def _range_warning(column: str, low: float, high: float) -> None:
        if column not in work.columns:
            return
        min_value, max_value = _series_numeric_bounds(work[column])
        if min_value is None or max_value is None:
            warnings.append(f"{column}: all values null or non-numeric")
            return
        if min_value < low or max_value > high:
            warnings.append(
                f"{column}: out of range [{low}, {high}] min={min_value:.6f} max={max_value:.6f}"
            )

    def _positive_warning(column: str, *, allow_zero: bool) -> None:
        if column not in work.columns:
            return
        min_value, _ = _series_numeric_bounds(work[column])
        if min_value is None:
            warnings.append(f"{column}: all values null or non-numeric")
            return
        if allow_zero:
            if min_value < 0.0:
                warnings.append(f"{column}: negative value detected min={min_value:.6f}")
        elif min_value <= 0.0:
            warnings.append(f"{column}: non-positive value detected min={min_value:.6f}")

    _range_warning("rsi14", 0.0, 100.0)
    _range_warning("adx14", 0.0, 100.0)
    _positive_warning("atr14", allow_zero=False)
    _positive_warning("ema20", allow_zero=False)
    _positive_warning("ema50", allow_zero=False)
    _positive_warning("ema200", allow_zero=False)
    _positive_warning("volume_ratio20", allow_zero=True)
    _positive_warning("close", allow_zero=False)

    for column in work.columns:
        if column.startswith("_"):
            continue
        series = work[column]
        if series.null_count() == work.height:
            warnings.append(f"{column}: column is entirely null")
            continue
        if column in _EXPECTED_ZERO_COLUMNS:
            continue
        dtype = work.schema.get(column)
        if dtype is None or not (
            getattr(dtype, "is_numeric", lambda: False)() or dtype in {pl.Boolean}
        ):
            continue
        numeric = series.cast(pl.Float64, strict=False).drop_nulls()
        if numeric.is_empty():
            continue
        try:
            min_value = float(numeric.min())
            max_value = float(numeric.max())
        except (TypeError, ValueError):
            continue
        if min_value == 0.0 and max_value == 0.0:
            warnings.append(f"{column}: column is entirely 0.0")
    return warnings


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
) -> pl.DataFrame:
    """_prepare_frame with LRU cache keyed on (symbol, interval, close_time)."""
    if frame.is_empty() or "close_time" not in frame.columns or "close" not in frame.columns:
        result = _enrich_with_ws_data(
            _prepare_frame(frame),
            symbol,
            ws_manager if interval == "15m" else None,
        )
        for warning in _sanity_check_prepared_frame(result, symbol, interval):
            LOG.info("prepared frame sanity warning | %s", warning)
        return result

    last = frame.row(-1, named=True)
    first = frame.row(0, named=True)
    try:
        first_close_time_ns = _timestamp_ns(first["close_time"])
        close_time_ns = _timestamp_ns(last["close_time"])
    except (KeyError, TypeError, ValueError, OverflowError):
        result = _prepare_frame(frame)
        for warning in _sanity_check_prepared_frame(result, symbol, interval):
            LOG.info("prepared frame sanity warning | %s", warning)
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
    )
    for warning in _sanity_check_prepared_frame(result, symbol, interval):
        LOG.info("prepared frame sanity warning | %s", warning)
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
        except Exception:
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
        except Exception:
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

    work = add_microstructure_features(work)
    return work.with_columns(
        [
            pl.col("signed_order_flow").fill_null(0.0).fill_nan(0.0),
            pl.col("tob_imbalance").fill_null(0.0).fill_nan(0.0),
            pl.col("microprice_deviation_pct").fill_null(0.0).fill_nan(0.0),
        ]
    )


def _to_polars(df: object) -> pl.DataFrame:
    """Normalize supported frame-like values to Polars."""
    if isinstance(df, pl.DataFrame):
        return df
    if type(df).__module__.startswith("pandas"):
        raise TypeError("prepare_symbol expects Polars frames; pandas inputs are unsupported")
    return pl.DataFrame(cast(Any, df))


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
    import logging

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
    work_15m = _cached_prepare_frame(
        _to_polars(frames.df_15m),
        symbol=sym,
        interval="15m",
        ws_manager=ws_manager,
    )
    fallback_book = (frames.bid_price, frames.ask_price, frames.bid_qty, frames.ask_qty)
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
    work_4h = None
    if frames.df_4h is not None and not frames.df_4h.is_empty():
        work_4h = _cached_prepare_frame(_to_polars(frames.df_4h), symbol=sym, interval="4h")

    work_len_1h = len(work_1h) if work_1h is not None else 0
    work_len_15m = len(work_15m) if work_15m is not None else 0
    work_len_5m = len(work_5m) if work_5m is not None else 0
    work_len_4h = len(work_4h) if work_4h is not None else 0

    if min(work_len_1h, work_len_15m) < 30:
        _log.info(
            "%s: insufficient processed data | work_1h=%d work_15m=%d optional_5m=%d optional_4h=%d need=30",
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
        "%s: prepared symbol successfully | primary_timeframe=%s work_primary=%d work_15m=%d work_1h=%d work_5m=%d optional_4h=%d",
        sym,
        primary_timeframe,
        len(primary_work) if primary_work is not None else 0,
        work_len_15m,
        work_len_1h,
        work_len_5m,
        work_len_4h,
    )

    # Calculate spread
    spread_bps = None
    if (
        frames.bid_price is not None
        and frames.ask_price is not None
        and frames.bid_price > 0
        and frames.ask_price > 0
    ):
        midpoint = (frames.bid_price + frames.ask_price) / 2.0
        if midpoint > 0:
            spread_bps = ((frames.ask_price - frames.bid_price) / midpoint) * 10_000.0
    book_depth_imbalance = depth_imbalance_from_book(
        bid_qty=frames.bid_qty,
        ask_qty=frames.ask_qty,
        delta_ratio=None,
    )
    book_microprice_bias = microprice_bias_from_book(
        bid=frames.bid_price,
        ask=frames.ask_price,
        bid_qty=frames.bid_qty,
        ask_qty=frames.ask_qty,
        delta_ratio=None,
    )

    work_4h_frame = work_4h if work_4h is not None else pl.DataFrame()
    regime = _market_regime(work_4h_frame, work_1h=work_1h, work_15m=work_15m)

    return PreparedSymbol(
        universe=universe_symbol,
        work_1h=work_1h,
        work_15m=work_15m,
        bid_price=frames.bid_price,
        ask_price=frames.ask_price,
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
        primary_timeframe=primary_timeframe,
        context_timeframes=context_timeframes,
        settings=settings,
    )
