"""Spec-column enrichment and confirmed pivot extraction (shared TA kernel).

Moved out of ``bot.strategies._common`` so both the main bot strategies and the
independent hunt project can use the same spec columns / divergence pivots.
"""

from __future__ import annotations

import math
import threading

import polars as pl

from engine.features.prepare import _swing_points
from engine.features.shared import wilder_mean

_SPEC_COLUMN_CACHE: dict[tuple[object, ...], pl.DataFrame] = {}
_SPEC_COLUMN_CACHE_MAX = 512
_SPEC_COLUMN_CACHE_LOCK = threading.Lock()


def as_float(value: object, default: float = 0.0) -> float:
    try:
        numeric = float(value) if value is not None else default
    except TypeError, ValueError:
        return default
    return numeric if math.isfinite(numeric) else default


def required_columns(frame: pl.DataFrame, columns: tuple[str, ...]) -> list[str]:
    return [column for column in columns if column not in frame.columns]


def _feature_or_expr(
    frame: pl.DataFrame,
    source_column: str,
    fallback: pl.Expr,
    alias: str,
) -> pl.Expr:
    if source_column in frame.columns:
        return pl.col(source_column).cast(pl.Float64, strict=False).alias(alias)
    return fallback.alias(alias)


def _spec_cache_key(frame: pl.DataFrame) -> tuple[object, ...] | None:
    if frame.is_empty() or "close" not in frame.columns:
        return None
    tail_closes = tuple(
        round(as_float(value), 8) for value in frame.select("close").tail(5).to_series().to_list()
    )
    tail_time = frame.item(-1, "close_time") if "close_time" in frame.columns else None
    return (frame.height, tail_closes, str(tail_time), tuple(frame.columns))


def with_spec_columns(frame: pl.DataFrame) -> pl.DataFrame:
    """Add strict spec columns while reusing prepared feature columns when present."""
    if frame.is_empty():
        return frame
    required = required_columns(frame, ("open", "high", "low", "close", "volume"))
    if required:
        return frame
    if "_spec_idx" in frame.columns:
        return frame
    cache_key = _spec_cache_key(frame)
    if cache_key is not None:
        with _SPEC_COLUMN_CACHE_LOCK:
            cached = _SPEC_COLUMN_CACHE.get(cache_key)
            if cached is not None:
                return cached.clone()

    work = frame.with_row_index("_spec_idx")
    prev_close = pl.col("close").shift(1)
    tr_expr = pl.max_horizontal(
        pl.col("high") - pl.col("low"),
        (pl.col("high") - prev_close).abs(),
        (pl.col("low") - prev_close).abs(),
    )
    if {"taker_buy_base_volume", "volume"}.issubset(set(work.columns)):
        spec_delta = 2.0 * pl.col("taker_buy_base_volume") - pl.col("volume")
    elif "delta_ratio" in work.columns:
        spec_delta = (pl.col("delta_ratio") - 0.5) * 2.0 * pl.col("volume")
    else:
        spec_delta = pl.lit(None).cast(pl.Float64)

    if "rsi14" in work.columns:
        rsi_expr: pl.Expr | pl.Series = (
            pl.col("rsi14").cast(pl.Float64, strict=False).alias("rsi14")
        )
    else:
        close = work["close"].cast(pl.Float64, strict=False)
        delta = close.diff()
        gain = delta.clip(lower_bound=0.0)
        loss = (-delta).clip(lower_bound=0.0)
        avg_gain = wilder_mean(gain, period=14, name="spec_avg_gain", seed_offset=1)
        avg_loss = wilder_mean(loss, period=14, name="spec_avg_loss", seed_offset=1)
        raw_rsi = (100.0 - (100.0 / (1.0 + (avg_gain / avg_loss)))).fill_nan(50.0)
        rsi_expr = (
            pl.when((avg_loss == 0) & (avg_gain > 0))
            .then(100.0)
            .when((avg_gain == 0) & (avg_loss > 0))
            .then(0.0)
            .when((avg_gain == 0) & (avg_loss == 0))
            .then(50.0)
            .otherwise(raw_rsi)
            .alias("rsi14")
        )

    if "vwap" in work.columns:
        vwap_expr = pl.col("vwap").cast(pl.Float64, strict=False).alias("vwap")
    else:
        time_column = next(
            (column for column in ("open_time", "time", "close_time") if column in work.columns),
            None,
        )
        typical_price = (pl.col("high") + pl.col("low") + pl.col("close")) / 3.0
        if (
            time_column is not None
            and getattr(work.schema.get(time_column), "is_temporal", lambda: False)()
        ):
            vwap_expr = (
                (typical_price * pl.col("volume")).cum_sum().over(pl.col(time_column).dt.date())
                / pl.col("volume").cum_sum().over(pl.col(time_column).dt.date())
            ).alias("vwap")
        else:
            vwap_expr = (
                (typical_price * pl.col("volume")).cum_sum() / pl.col("volume").cum_sum()
            ).alias("vwap")

    tr_series: pl.Series | None = None
    if "atr14" not in work.columns or "atr20" not in work.columns:
        tr_series = work.select(tr_expr.alias("_tr")).to_series()

    if "atr14" in work.columns:
        spec_atr14_value: pl.Expr | pl.Series = (
            pl.col("atr14").cast(pl.Float64, strict=False).alias("spec_atr14")
        )
    else:
        spec_atr14_value = wilder_mean(tr_series, period=14, name="spec_atr14")

    if "atr20" in work.columns:
        spec_atr20_value: pl.Expr | pl.Series = (
            pl.col("atr20").cast(pl.Float64, strict=False).alias("spec_atr20")
        )
    else:
        spec_atr20_value = wilder_mean(tr_series, period=20, name="spec_atr20")

    pass1: list[pl.Expr | pl.Series] = [
        tr_expr.alias("spec_tr"),
        spec_atr14_value,
        spec_atr20_value,
        _feature_or_expr(
            work,
            "volume_mean20",
            pl.col("volume").rolling_mean(20),
            "spec_volume_mean20",
        ),
        _feature_or_expr(
            work,
            "ema20",
            pl.col("close").ewm_mean(span=20, adjust=False),
            "spec_ema20",
        ),
        _feature_or_expr(
            work,
            "ema21",
            pl.col("close").ewm_mean(span=21, adjust=False),
            "spec_ema21",
        ),
        _feature_or_expr(
            work,
            "ema50",
            pl.col("close").ewm_mean(span=50, adjust=False),
            "spec_ema50",
        ),
        _feature_or_expr(
            work,
            "ema200",
            pl.col("close").ewm_mean(span=200, adjust=False),
            "spec_ema200",
        ),
        _feature_or_expr(work, "sma20", pl.col("close").rolling_mean(20), "spec_sma20"),
        (pl.col("high").shift(1).rolling_max(20)).alias("spec_prev_high20"),
        (pl.col("low").shift(1).rolling_min(20)).alias("spec_prev_low20"),
        (pl.col("high").shift(1).rolling_max(30)).alias("spec_prev_high30"),
        (pl.col("low").shift(1).rolling_min(30)).alias("spec_prev_low30"),
        (pl.col("high") - pl.col("low")).alias("spec_range"),
        (pl.col("close") - pl.col("open")).abs().alias("spec_body"),
        (pl.col("high") - pl.max_horizontal(pl.col("open"), pl.col("close"))).alias(
            "spec_upper_wick"
        ),
        (pl.min_horizontal(pl.col("open"), pl.col("close")) - pl.col("low")).alias(
            "spec_lower_wick"
        ),
        spec_delta.alias("spec_delta"),
        rsi_expr,
        vwap_expr,
    ]
    work = work.with_columns(pass1)

    bb_std = pl.col("close").rolling_std(window_size=20, ddof=0)
    if "kc_upper_15" in work.columns and "kc_lower_15" in work.columns:
        kc15_upper_value = pl.col("kc_upper_15").cast(pl.Float64, strict=False)
        kc15_lower_value = pl.col("kc_lower_15").cast(pl.Float64, strict=False)
    else:
        kc15_upper_value = pl.col("spec_ema20") + 1.5 * pl.col("spec_atr20")
        kc15_lower_value = pl.col("spec_ema20") - 1.5 * pl.col("spec_atr20")
    if "kc_upper" in work.columns and "kc_lower" in work.columns:
        kc20_upper_value = pl.col("kc_upper").cast(pl.Float64, strict=False)
        kc20_lower_value = pl.col("kc_lower").cast(pl.Float64, strict=False)
    else:
        kc20_upper_value = pl.col("spec_ema20") + 2.0 * pl.col("spec_atr14")
        kc20_lower_value = pl.col("spec_ema20") - 2.0 * pl.col("spec_atr14")
    if "bb_upper" in work.columns and "bb_lower" in work.columns:
        bb_upper_value = pl.col("bb_upper").cast(pl.Float64, strict=False)
        bb_lower_value = pl.col("bb_lower").cast(pl.Float64, strict=False)
    else:
        bb_upper_value = pl.col("spec_sma20") + 2.0 * bb_std
        bb_lower_value = pl.col("spec_sma20") - 2.0 * bb_std

    work = work.with_columns(
        [
            bb_upper_value.alias("spec_bb_upper"),
            bb_lower_value.alias("spec_bb_lower"),
            kc15_upper_value.alias("spec_kc15_upper"),
            kc15_lower_value.alias("spec_kc15_lower"),
            kc20_upper_value.alias("spec_kc20_upper"),
            kc20_lower_value.alias("spec_kc20_lower"),
            ((bb_upper_value < kc15_upper_value) & (bb_lower_value > kc15_lower_value)).alias(
                "spec_squeeze"
            ),
            (
                pl.col("spec_body")
                / pl.when(pl.col("spec_range") > 0.0).then(pl.col("spec_range")).otherwise(1e-8)
            ).alias("spec_body_ratio"),
            (
                pl.col("spec_upper_wick")
                / pl.when(pl.col("spec_body") > 0.0).then(pl.col("spec_body")).otherwise(1e-8)
            ).alias("spec_upper_wick_ratio"),
            (
                pl.col("spec_lower_wick")
                / pl.when(pl.col("spec_body") > 0.0).then(pl.col("spec_body")).otherwise(1e-8)
            ).alias("spec_lower_wick_ratio"),
            pl.col("spec_delta").abs().rolling_mean(20).alias("spec_abs_delta_mean20"),
            pl.col("spec_delta").rolling_std(20, ddof=1).alias("spec_delta_std20"),
            _spec_cvd_expr(work).alias("spec_cvd"),
        ]
    )
    if cache_key is not None:
        with _SPEC_COLUMN_CACHE_LOCK:
            if len(_SPEC_COLUMN_CACHE) >= _SPEC_COLUMN_CACHE_MAX:
                _SPEC_COLUMN_CACHE.pop(next(iter(_SPEC_COLUMN_CACHE)))
            _SPEC_COLUMN_CACHE[cache_key] = work
    return work


def _spec_cvd_expr(frame: pl.DataFrame) -> pl.Expr:
    if "rolling_cvd_24h" in frame.columns:
        return pl.col("rolling_cvd_24h").cast(pl.Float64, strict=False)
    if "session_cvd" in frame.columns:
        return pl.col("session_cvd").cast(pl.Float64, strict=False)
    time_column = next(
        (
            column
            for column in ("close_time", "time", "open_time")
            if column in frame.columns
            and getattr(frame.schema.get(column), "is_temporal", lambda: False)()
        ),
        None,
    )
    delta = pl.col("spec_delta").fill_null(0.0)
    if time_column is not None:
        return delta.cum_sum().over(pl.col(time_column).dt.date())
    return delta.cum_sum()


def _pivot_rows(
    work: pl.DataFrame,
    *,
    price_column: str,
    indicator_column: str,
    pivot: str,
    max_lookback: int = 50,
) -> list[dict[str, float]]:
    if work.height < 7 or price_column not in work.columns or indicator_column not in work.columns:
        return []
    current_idx = int(work.item(-1, "_spec_idx"))
    high_mask, low_mask = _swing_points(work, n=2, include_unconfirmed_tail=False)
    mask = low_mask if pivot == "low" else high_mask
    # Live-safe divergence pivots: exclude the two tail bars before comparing neighbors.
    mask = mask & (work["_spec_idx"] <= current_idx - 2)
    confirmed = work.filter(mask).to_dicts()
    return [
        {
            "idx": float(row["_spec_idx"]),
            "price": as_float(row.get(price_column)),
            "indicator": as_float(row.get(indicator_column)),
        }
        for row in confirmed[-8:]
        if current_idx - int(row["_spec_idx"]) <= max_lookback
    ]
