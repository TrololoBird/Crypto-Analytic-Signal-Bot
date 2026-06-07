"""Per-frame OHLCV indicator pipeline (_prepare_frame)."""

from __future__ import annotations

import importlib
from datetime import UTC, date, datetime
from importlib import util as importlib_util
from typing import TYPE_CHECKING, Any, cast

import numpy as np
import polars as pl
import structlog

from bot.runtime.errors import DEFENSIVE_EXC

from .candle_patterns import add_candle_pattern_columns
from .microstructure import add_microstructure_features
from .prepare_columns import group_active
from .shared import supertrend_series, wilder_mean
from .structure import (
    hull_moving_average as _hull_moving_average_external,
)
from .structure import (
    weighted_moving_average as _weighted_moving_average_external,
)

if TYPE_CHECKING:
    from collections.abc import Iterable

    from ..domain.schemas import SymbolFrames

# Optional polars_ta backend for a few indicators (EMA/ROC/OBV).
# Pure-Polars fallbacks are canonical; no TA-Lib / pandas on the live path.
try:
    _plta_module = importlib_util.find_spec("polars_ta.ta")
except ImportError, ModuleNotFoundError:
    _plta_module = None

if _plta_module is not None:
    plta = cast("Any", importlib.import_module("polars_ta.ta"))
    _HAS_POLARS_TA = True
else:
    plta = cast("Any", None)
    _HAS_POLARS_TA = False
_USE_POLARS_TA_BACKEND = _HAS_POLARS_TA

try:
    _polars_ols_module = importlib_util.find_spec("polars_ols")
except ImportError, ModuleNotFoundError:
    _polars_ols_module = None

if _polars_ols_module is not None:
    _polars_ols = cast("Any", importlib.import_module("polars_ols"))
    _polars_ols_ls = cast("Any", importlib.import_module("polars_ols.least_squares"))
    _HAS_POLARS_OLS = True
else:
    _polars_ols = cast("Any", None)
    _polars_ols_ls = cast("Any", None)
    _HAS_POLARS_OLS = False

# Compatibility name for the decomposed feature modules/tests. This tracks the
LOG = structlog.get_logger("bot.features.prepare_frame")
_ADVANCED_FALLBACKS_LOGGED: set[str] = set()
_FRAME_PREP_FALLBACKS: list[str] = []


def reset_frame_indicator_fallbacks() -> None:
    _FRAME_PREP_FALLBACKS.clear()


def take_frame_indicator_fallbacks() -> list[str]:
    flags = [f"indicator_fallback:{name}" for name in _FRAME_PREP_FALLBACKS]
    _FRAME_PREP_FALLBACKS.clear()
    return flags


def _log_indicator_fallback(indicator: str, exc: Exception) -> None:
    if indicator not in _FRAME_PREP_FALLBACKS:
        _FRAME_PREP_FALLBACKS.append(indicator)
    if indicator in _ADVANCED_FALLBACKS_LOGGED:
        LOG.debug("advanced indicator fallback reused", indicator=indicator, error=str(exc))
        return
    _ADVANCED_FALLBACKS_LOGGED.add(indicator)
    LOG.info("advanced indicator fallback activated", indicator=indicator, error=str(exc))


_FrameCacheValue = float | None
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


def _clean_non_finite(series: pl.Series, *, fill: float) -> pl.Series:
    """Replace NaN/inf/null values with a stable fill value."""
    return series.replace([float("inf"), float("-inf")], None).fill_nan(fill).fill_null(fill)


def _timestamp_ns(value: object) -> int:
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if hasattr(value, "timestamp"):
        return int(value.timestamp() * 1e9)
    return int(cast("Any", value))


def _tail_value_signature(row: dict[str, object]) -> tuple[_FrameCacheValue, ...]:
    values: list[_FrameCacheValue] = []
    for column in _FRAME_CACHE_TAIL_COLUMNS:
        raw = row.get(column)
        if raw is None:
            values.append(None)
            continue
        try:
            value = float(cast("Any", raw))
        except TypeError, ValueError:
            values.append(None)
            continue
        values.append(None if value != value else value)
    return tuple(values)


def _materialize_series(
    value: pl.Series | pl.Expr | float,
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
    except TypeError, ValueError:
        return numeric.rename(name)
    if max_float <= 1.5 and min_float >= -0.01:
        return (numeric * 100.0).rename(name)
    return numeric.rename(name)


def _numeric_item(df: pl.DataFrame, row: int, column: str, default: float = 0.0) -> float:
    try:
        value = df.item(row, column)
    except IndexError, ValueError:
        return default
    try:
        return default if value is None else float(value)
    except TypeError, ValueError:
        return default


def _as_float_like(value: object, default: float = 0.0) -> float:
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    return default


def _as_optional_float(value: object) -> float | None:
    try:
        numeric = float(cast("Any", value)) if value is not None else None
    except TypeError, ValueError:
        return None
    if numeric is None or not np.isfinite(numeric):
        return None
    return numeric


def _finite_float(value: object, default: float = 0.0) -> float:
    numeric = _as_optional_float(value)
    return default if numeric is None else numeric


def min_required_bars(
    *,
    min_bars_15m: int = 500,
    min_bars_1h: int = 300,
    min_bars_5m: int = 200,
    min_bars_4h: int = 300,
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
        except DEFENSIVE_EXC as exc:
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


def _adx_components(df: pl.DataFrame, period: int = 14) -> tuple[pl.Series, pl.Series, pl.Series]:
    """ADX with directional indicators (+DI / -DI)."""
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
    plus_di = _materialize_series(
        _clean_non_finite(
            100.0 * wilder_mean(plus_dm, period=period, name="plus_dm_smoothed") / atr_safe,
            fill=0.0,
        ).clip(0.0, 100.0),
        df=df,
        name=f"plus_di{period}",
    )
    minus_di = _materialize_series(
        _clean_non_finite(
            100.0 * wilder_mean(minus_dm, period=period, name="minus_dm_smoothed") / atr_safe,
            fill=0.0,
        ).clip(0.0, 100.0),
        df=df,
        name=f"minus_di{period}",
    )

    di_sum = (plus_di + minus_di).replace(0.0, None)
    dx = _clean_non_finite(100.0 * (plus_di - minus_di).abs() / di_sum, fill=0.0)
    adx = _materialize_series(
        _clean_non_finite(
            wilder_mean(dx, period=period, name=f"adx{period}", seed_offset=period - 1),
            fill=0.0,
        ).clip(0.0, 100.0),
        df=df,
        name=f"adx{period}",
    )
    return adx, plus_di, minus_di


def _adx(df: pl.DataFrame, period: int = 14) -> pl.Series:
    adx, _plus_di, _minus_di = _adx_components(df, period=period)
    return adx


def _vwap_session_key(value: object) -> date | None:
    if isinstance(value, datetime):
        return value.astimezone(UTC).date() if value.tzinfo else value.date()
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
    except TypeError, ValueError:
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


def _session_time_column(df: pl.DataFrame) -> str | None:
    return next(
        (
            column
            for column in ("close_time", "time", "open_time")
            if column in df.columns and _is_temporal_dtype(df.schema.get(column))
        ),
        None,
    )


def add_session_cvd(df: pl.DataFrame) -> pl.DataFrame:
    """Cumulative volume delta reset at each UTC calendar date (session CVD)."""
    if df.is_empty():
        return df
    if {"taker_buy_base_volume", "volume"}.issubset(df.columns):
        bar_delta = 2.0 * pl.col("taker_buy_base_volume") - pl.col("volume")
    elif {"delta_ratio", "volume"}.issubset(df.columns):
        bar_delta = (pl.col("delta_ratio") - 0.5) * 2.0 * pl.col("volume")
    else:
        return df.with_columns(pl.lit(0.0).alias("session_cvd"))

    filled_delta = bar_delta.fill_null(0.0).fill_nan(0.0)
    time_column = _session_time_column(df)
    if time_column is not None:
        temp = df.with_columns(
            [
                filled_delta.alias("_cvd_bar_delta"),
                pl.col(time_column).dt.date().alias("_cvd_session"),
            ]
        )
        return temp.with_columns(
            pl.col("_cvd_bar_delta").cum_sum().over("_cvd_session").alias("session_cvd")
        ).drop("_cvd_bar_delta", "_cvd_session")

    return df.with_columns(filled_delta.cum_sum().alias("session_cvd"))


def _roc(df: pl.DataFrame, period: int = 10) -> pl.Series:
    if _USE_POLARS_TA_BACKEND and _HAS_POLARS_TA and hasattr(plta, "ROC"):
        try:
            return _materialize_series(
                plta.ROC(pl.col("close"), timeperiod=int(period)),
                df=df,
                name=f"roc{period}",
            )
        except DEFENSIVE_EXC as exc:
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
    hh = df["high"].rolling_max(window_size=period)
    ll = df["low"].rolling_min(window_size=period)
    width = (hh - ll).clip(lower_bound=1e-9)
    price_norm = ((df["close"] - ll) / width).fill_nan(0.5)
    raw_arr = (price_norm * 2.0 - 1.0).clip(-0.999, 0.999).to_numpy()
    size = raw_arr.shape[0]
    values = np.zeros(size, dtype=np.float64)
    fisher = np.zeros(size, dtype=np.float64)
    for i in range(size):
        prev_v = values[i - 1] if i > 0 else 0.0
        smoothed = 0.33 * float(raw_arr[i]) + 0.67 * prev_v
        smoothed = float(np.clip(smoothed, -0.999, 0.999))
        values[i] = smoothed
        prev_f = fisher[i - 1] if i > 0 else 0.0
        fisher[i] = 0.5 * np.log((1.0 + smoothed) / (1.0 - smoothed)) + 0.5 * prev_f
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
    squeeze_no = pl.DataFrame({"on": squeeze_on, "off": squeeze_off}).select(
        (pl.lit(1.0) - pl.max_horizontal("on", "off")).clip(0.0, 1.0).alias("squeeze_no")
    )["squeeze_no"]
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


def _stochastic_rsi(df: pl.DataFrame, period: int = 14) -> pl.Series:
    """Stochastic RSI = (RSI - min(RSI, N)) / (max(RSI, N) - min(RSI, N))."""
    rsi = _rsi(df, period=period)
    min_rsi = rsi.rolling_min(window_size=period)
    max_rsi = rsi.rolling_max(window_size=period)
    denom = max_rsi - min_rsi
    stoch = (rsi - min_rsi) / denom.replace(0.0, float("nan"))
    return stoch.fill_nan(0.5).fill_null(0.5).alias("stoch_rsi14")


def _ichimoku_cloud(df: pl.DataFrame) -> pl.DataFrame:
    """Compute Ichimoku Cloud components."""
    high9 = df["high"].rolling_max(window_size=9)
    low9 = df["low"].rolling_min(window_size=9)
    tenkan = (high9 + low9) / 2.0
    high26 = df["high"].rolling_max(window_size=26)
    low26 = df["low"].rolling_min(window_size=26)
    kijun = (high26 + low26) / 2.0
    senkou_a = (tenkan + kijun) / 2.0
    high52 = df["high"].rolling_max(window_size=52)
    low52 = df["low"].rolling_min(window_size=52)
    senkou_b = (high52 + low52) / 2.0
    chikou = df["close"].shift(26)
    return df.select(
        tenkan.alias("tenkan"),
        kijun.alias("kijun"),
        senkou_a.alias("senkou_a"),
        senkou_b.alias("senkou_b"),
        chikou.alias("chikou"),
    )


def _kama(df: pl.DataFrame, period: int = 10, fast: int = 2, slow: int = 30) -> pl.Series:
    """Kaufman Adaptive Moving Average."""
    close = df["close"]
    change = close.diff(period)
    volatility = close.diff().abs().rolling_sum(window_size=period)
    er = (change.abs() / volatility.replace(0.0, float("nan"))).fill_nan(0.0)
    fastest = 2.0 / (fast + 1.0)
    slowest = 2.0 / (slow + 1.0)
    sc_raw = er * (fastest - slowest) + slowest
    sc = sc_raw**2
    close_np = close.to_numpy()
    sc_np = sc.to_numpy()
    kama_np = np.empty_like(close_np)
    if len(kama_np) > 0:
        kama_np[0] = close_np[0]
        for i in range(1, len(kama_np)):
            kama_np[i] = kama_np[i - 1] + sc_np[i] * (close_np[i] - kama_np[i - 1])
    return pl.Series("kama10", kama_np).fill_nan(float("nan"))


def _heikin_ashi(df: pl.DataFrame) -> pl.DataFrame:
    """Heikin Ashi candles from OHLC."""
    ha_close = (df["open"] + df["high"] + df["low"] + df["close"]) / 4.0
    ha_open = ha_close.shift(1).fill_null(ha_close[0]).ewm_mean(alpha=0.5, adjust=False)
    ha_high = pl.max_horizontal(df["high"], ha_open, ha_close)
    ha_low = pl.min_horizontal(df["low"], ha_open, ha_close)
    return df.select(
        ha_open.alias("ha_open"),
        ha_high.alias("ha_high"),
        ha_low.alias("ha_low"),
        ha_close.alias("ha_close"),
    )


def _add_advanced_indicators(
    df: pl.DataFrame,
    *,
    active_groups: frozenset[str] | None = None,
) -> pl.DataFrame:
    """Add advanced technical indicators using pure Polars implementations."""
    result = df

    # --- SuperTrend ---------------------------------------------------------
    if group_active(active_groups, "supertrend"):
        st, st_dir = supertrend_series(df, period=10, multiplier=3.0)
        result = result.with_columns(
            [
                st.alias("supertrend"),
                st_dir.alias("supertrend_dir"),
            ]
        )

    # --- OBV ---------------------------------------------------------------
    if group_active(active_groups, "obv"):
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
        except DEFENSIVE_EXC as exc:
            _log_indicator_fallback("obv", exc)
            result = result.with_columns(
                [
                    pl.lit(0.0).alias("obv"),
                    pl.lit(0.0).alias("obv_ema20"),
                    pl.lit(0.0).alias("obv_above_ema"),
                ]
            )

    # --- Bollinger Bands - pure Polars implementation ------------------------
    if group_active(active_groups, "bb"):
        upper, middle, lower = _bollinger_bands(df["close"], period=20, nbdev=2.0)
        bb_pct_b = (df["close"] - lower) / (upper - lower)
        middle_safe = _clean_non_finite(middle.abs(), fill=1e-10).clip(lower_bound=1e-10)
        bb_width = (upper - lower) / middle_safe * 100.0
        bb_width_clean = _clean_non_finite(bb_width, fill=0.0)
        result = result.with_columns(
            [
                _clean_non_finite(bb_pct_b, fill=0.5).alias("bb_pct_b"),
                bb_width_clean.alias("bb_width"),
                (bb_width_clean.rolling_rank(window_size=50, method="average") / 50.0).alias(
                    "bb_width_pctile50"
                ),
            ]
        )

    # --- Keltner Channels - pure Polars implementation -----------------------
    if group_active(active_groups, "keltner"):
        kc_upper, _kc_middle, kc_lower = _keltner_channels(df, period=20, multiplier=2.0)
        close_safe = _clean_non_finite(df["close"].abs(), fill=1e-10).clip(lower_bound=1e-10)
        kc_width = (kc_upper - kc_lower) / close_safe
        result = result.with_columns(
            [
                kc_upper.alias("kc_upper"),
                kc_lower.alias("kc_lower"),
                _clean_non_finite(kc_width, fill=0.04).alias("kc_width"),
            ]
        )

    # --- Stochastic RSI (п.10) ------------------------------------------------
    if group_active(active_groups, "stoch_rsi"):
        stoch_rsi = _stochastic_rsi(df, period=14)
        result = result.with_columns(stoch_rsi.alias("stoch_rsi14"))

    # --- Ichimoku Cloud (п.11) ------------------------------------------------
    if group_active(active_groups, "ichimoku"):
        ichi = _ichimoku_cloud(df)
        if ichi is not None and not ichi.is_empty():
            for col in ichi.columns:
                result = result.with_columns(ichi[col].alias(col))

    # --- KAMA (Kaufman Adaptive Moving Average, п.13) -------------------------
    if group_active(active_groups, "kama"):
        kama = _kama(df, period=10, fast=2, slow=30)
        result = result.with_columns(kama.alias("kama10"))

    # --- Heikin Ashi candles (п.15) -------------------------------------------
    if group_active(active_groups, "heikin_ashi"):
        ha = _heikin_ashi(df)
        if ha is not None and not ha.is_empty():
            for col in ha.columns:
                result = result.with_columns(ha[col].alias(col))

    # --- HMA (Hull Moving Average) --------------------------------------------
    if group_active(active_groups, "hma"):
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
    if group_active(active_groups, "psar"):
        psar_long, psar_short, psar_reversal = _parabolic_sar(df, step=0.02, max_step=0.2)
        result = result.with_columns(
            [
                psar_long.alias("psar_long"),
                psar_short.alias("psar_short"),
                psar_reversal.alias("psar_reversal"),
            ]
        )

    # --- Aroon ---------------------------------------------------------------
    if group_active(active_groups, "aroon"):
        aroon_up, aroon_down, aroon_osc = _aroon(df, period=14)
        result = result.with_columns(
            [
                aroon_up.alias("aroon_up14"),
                aroon_down.alias("aroon_down14"),
                aroon_osc.alias("aroon_osc14"),
            ]
        )

    # --- Stochastic ---------------------------------------------------------
    if group_active(active_groups, "stoch"):
        stoch_k, stoch_d = _stochastic(df, period=14, smooth_k=3, smooth_d=3)
        result = result.with_columns(
            [
                stoch_k.alias("stoch_k14"),
                stoch_d.alias("stoch_d14"),
                (stoch_k - stoch_d).fill_nan(0.0).alias("stoch_h14"),
            ]
        )

    # --- CCI, Williams %R, MFI, CMF, Ultimate Oscillator --------------------
    if group_active(active_groups, "oscillators"):
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
    if group_active(active_groups, "fisher"):
        fisher, fisher_signal = _fisher_transform(df, period=10)
        result = result.with_columns(
            [
                fisher.alias("fisher"),
                fisher_signal.alias("fisher_signal"),
            ]
        )

    # --- Squeeze Momentum ----------------------------------------------------
    if group_active(active_groups, "squeeze"):
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
    if group_active(active_groups, "chandelier"):
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

    if group_active(active_groups, "volume_profile"):
        poc, vah, val = _volume_profile_levels(result, bins=12)
        result = result.with_columns(
            [
                pl.lit(poc).cast(pl.Float64).alias("volume_profile"),
                pl.lit(vah).cast(pl.Float64).alias("volume_profile_vah"),
                pl.lit(val).cast(pl.Float64).alias("volume_profile_val"),
            ]
        )

    if group_active(active_groups, "pivot_points"):
        pp, r1, r2, s1, s2 = _classic_pivot_points(result)
        result = result.with_columns(
            [
                pl.lit(pp).cast(pl.Float64).alias("pivot_point"),
                pl.lit(r1).cast(pl.Float64).alias("pivot_r1"),
                pl.lit(r2).cast(pl.Float64).alias("pivot_r2"),
                pl.lit(s1).cast(pl.Float64).alias("pivot_s1"),
                pl.lit(s2).cast(pl.Float64).alias("pivot_s2"),
            ]
        )

    # --- Z-Score and Slope -------------------------------------------------
    if group_active(active_groups, "zscore"):
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

    return result


def _volume_profile_levels(
    df: pl.DataFrame, bins: int = 12, *, value_area_pct: float = 0.70
) -> tuple[float | None, float | None, float | None]:
    """Scalar POC/VAH/VAL for the current frame window."""
    if df.is_empty() or not {"high", "low", "volume"}.issubset(df.columns):
        return None, None, None
    prices = ((df["high"] + df["low"]) / 2.0).cast(pl.Float64, strict=False)
    volumes = df["volume"].cast(pl.Float64, strict=False)
    valid_mask = prices.is_not_null() & prices.is_finite() & volumes.is_not_null() & (volumes > 0.0)
    v_prices = prices.filter(valid_mask)
    v_volumes = volumes.filter(valid_mask)
    if v_prices.is_empty():
        return None, None, None
    price_min = _as_optional_float(v_prices.min())
    price_max = _as_optional_float(v_prices.max())
    if price_min is None or price_max is None or price_max <= price_min:
        return price_max, price_max, price_min
    bucket_count = max(1, int(bins))
    bucket_size = (price_max - price_min) / bucket_count
    bucket_ids = (
        ((v_prices - price_min) / bucket_size).floor().cast(pl.Int32).clip(0, bucket_count - 1)
    )
    vol_by_bucket = (
        pl.DataFrame({"b": bucket_ids, "v": v_volumes}).group_by("b").agg(pl.col("v").sum())
    )
    if vol_by_bucket.is_empty():
        return price_min, price_max, price_min
    rows = sorted(vol_by_bucket.iter_rows(named=True), key=lambda row: int(row["b"]))
    total_volume = sum(float(row["v"] or 0.0) for row in rows)
    if total_volume <= 0.0:
        return price_min, price_max, price_min
    poc_row = max(rows, key=lambda row: float(row["v"] or 0.0))
    poc_bucket = int(poc_row["b"])
    poc = float(price_min + (poc_bucket + 0.5) * bucket_size)
    target_volume = total_volume * max(0.5, min(value_area_pct, 0.95))
    accumulated = float(poc_row["v"] or 0.0)
    included = {poc_bucket}
    left = poc_bucket - 1
    right = poc_bucket + 1
    while accumulated < target_volume and (left >= 0 or right < bucket_count):
        left_vol = next((float(row["v"] or 0.0) for row in rows if int(row["b"]) == left), 0.0)
        right_vol = next((float(row["v"] or 0.0) for row in rows if int(row["b"]) == right), 0.0)
        if right_vol >= left_vol and right < bucket_count:
            accumulated += right_vol
            included.add(right)
            right += 1
        elif left >= 0:
            accumulated += left_vol
            included.add(left)
            left -= 1
        else:
            break
    val = float(price_min + min(included) * bucket_size)
    vah = float(price_min + (max(included) + 1) * bucket_size)
    return poc, vah, val


def _volume_profile(df: pl.DataFrame, bins: int = 12) -> pl.Expr:
    poc, _vah, _val = _volume_profile_levels(df, bins=bins)
    return pl.lit(0.0 if poc is None else poc).cast(pl.Float64).alias("volume_profile")


def _classic_pivot_points(
    df: pl.DataFrame,
) -> tuple[float | None, float | None, float | None, float | None, float | None]:
    """Classic daily pivot points from the last ~24h of data (or frame range).

    Returns (PP, R1, R2, S1, S2). Returns (None, None, None, None, None) if
    insufficient data.
    """
    if df.is_empty() or not {"high", "low", "close"}.issubset(df.columns):
        return None, None, None, None, None
    tail = df.tail(min(df.height, 48))
    prev_high = float(tail["high"].max() or 0.0)
    prev_low = float(tail["low"].min() or 0.0)
    prev_close = float(tail["close"][-1] or 0.0)
    if prev_high <= 0.0 or prev_low <= 0.0 or prev_close <= 0.0:
        return None, None, None, None, None
    pp = (prev_high + prev_low + prev_close) / 3.0
    r1 = 2.0 * pp - prev_low
    r2 = pp + (prev_high - prev_low)
    s1 = 2.0 * pp - prev_high
    s2 = pp - (prev_high - prev_low)
    return pp, r1, r2, s1, s2


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
    except DEFENSIVE_EXC as exc:
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


def _prepare_frame(
    df: pl.DataFrame,
    *,
    active_groups: frozenset[str] | None = None,
) -> pl.DataFrame:
    """Compute all technical indicators for a single OHLCV DataFrame.

    Returns a new DataFrame with NaN-seeded rows dropped.
    All backward-compatible column names are preserved.

    When ``active_groups`` is set, optional indicator blocks may be skipped
    (see ``bot.features.prepare_columns``). ``None`` computes every group.
    """
    df = _coerce_temporal_columns(df)

    adx14, plus_di14, minus_di14 = _adx_components(df, 14)
    # Core indicators
    work = df.with_columns(
        [
            _ema(df, 20).alias("ema20"),
            _ema(df, 50).alias("ema50"),
            _ema(df, 200).alias("ema200"),
            _rsi(df, 14).alias("rsi14"),
            adx14.alias("adx14"),
            plus_di14.alias("plus_di14"),
            minus_di14.alias("minus_di14"),
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
            ((pl.col("close") - pl.col("vwap")) / pl.col("atr14"))
            .fill_nan(0.0)
            .alias("vwap_deviation_atr14"),
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

    work = add_session_cvd(work)

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
    work = _add_advanced_indicators(work, active_groups=active_groups)
    if group_active(active_groups, "microstructure"):
        work = add_microstructure_features(work)
    if group_active(active_groups, "ols"):
        work = _add_polars_ols_features(work)
    if group_active(active_groups, "tail_metrics"):
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
    if group_active(active_groups, "session"):
        work = _add_session_features(work)
    if group_active(active_groups, "candles"):
        work = add_candle_pattern_columns(work)

    # Drop rows with insufficient data
    # Filter where ema200 or donchian_low20 is null
    work = work.filter(pl.col("ema200").is_not_null() & pl.col("donchian_low20").is_not_null())

    return work


# ---------------------------------------------------------------------------
# 4h bias helper
# ---------------------------------------------------------------------------


__all__ = [
    "_add_advanced_indicators",
    "_as_float_like",
    "_as_optional_float",
    "_finite_float",
    "_numeric_item",
    "_prepare_frame",
    "add_session_cvd",
    "has_minimum_bars",
    "min_required_bars",
]
