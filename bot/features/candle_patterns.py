"""Unified candlestick pattern columns (polars_ta with Polars fallbacks)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

import polars as pl

from bot.runtime.errors import DEFENSIVE_EXC

if TYPE_CHECKING:
    pass

try:
    from importlib import util as importlib_util

    _candles_module = importlib_util.find_spec("polars_ta.candles")
except (ImportError, ModuleNotFoundError):
    _candles_module = None

if _candles_module is not None:
    _ptc = cast("Any", __import__("polars_ta.candles", fromlist=["candles"]))
    _HAS_POLARS_TA_CANDLES = True
else:
    _ptc = cast("Any", None)
    _HAS_POLARS_TA_CANDLES = False

_CANDLE_OUTPUT_COLUMNS: tuple[str, ...] = (
    "candle_doji",
    "candle_dragonfly",
    "candle_gravestone",
    "candle_bullish_engulfing",
    "candle_bearish_engulfing",
)


def _pure_polars_candle_exprs() -> list[pl.Expr]:
    open_ = pl.col("open")
    high = pl.col("high")
    low = pl.col("low")
    close = pl.col("close")
    body = (close - open_).abs()
    full_range = (high - low).clip(lower_bound=1e-9)
    body_ratio = body / full_range
    upper_shadow = high - pl.max_horizontal(open_, close)
    lower_shadow = pl.min_horizontal(open_, close) - low

    prev_open = open_.shift(1)
    prev_close = close.shift(1)
    prev_body_low = pl.min_horizontal(prev_open, prev_close)
    prev_body_high = pl.max_horizontal(prev_open, prev_close)
    curr_body_low = pl.min_horizontal(open_, close)
    curr_body_high = pl.max_horizontal(open_, close)

    doji = body_ratio <= 0.10
    dragonfly = (body_ratio <= 0.25) & (lower_shadow >= body * 2.0) & (upper_shadow <= body)
    gravestone = (body_ratio <= 0.25) & (upper_shadow >= body * 2.0) & (lower_shadow <= body)
    bullish_engulf = (
        (prev_close < prev_open)
        & (close > open_)
        & (curr_body_low <= prev_body_low)
        & (curr_body_high >= prev_body_high)
    )
    bearish_engulf = (
        (prev_close > prev_open)
        & (close < open_)
        & (curr_body_low <= prev_body_low)
        & (curr_body_high >= prev_body_high)
    )
    return [
        doji.cast(pl.Float64).alias("candle_doji"),
        dragonfly.cast(pl.Float64).alias("candle_dragonfly"),
        gravestone.cast(pl.Float64).alias("candle_gravestone"),
        bullish_engulf.cast(pl.Float64).alias("candle_bullish_engulfing"),
        bearish_engulf.cast(pl.Float64).alias("candle_bearish_engulfing"),
    ]


def add_candle_pattern_columns(df: pl.DataFrame) -> pl.DataFrame:
    """Add shared candle pattern flags used by SMC / liquidity strategies."""
    if df.is_empty() or not {"open", "high", "low", "close"}.issubset(df.columns):
        return df.with_columns([pl.lit(0.0).alias(name) for name in _CANDLE_OUTPUT_COLUMNS])

    if _HAS_POLARS_TA_CANDLES:
        try:
            open_ = pl.col("open")
            high = pl.col("high")
            low = pl.col("low")
            close = pl.col("close")
            prev_open = open_.shift(1)
            prev_close = close.shift(1)
            prev_body_low = pl.min_horizontal(prev_open, prev_close)
            prev_body_high = pl.max_horizontal(prev_open, prev_close)
            curr_body_low = pl.min_horizontal(open_, close)
            curr_body_high = pl.max_horizontal(open_, close)
            return df.with_columns(
                [
                    _ptc.doji(open_, high, low, close).cast(pl.Float64).alias("candle_doji"),
                    _ptc.dragonfly(open_, high, low, close)
                    .cast(pl.Float64)
                    .alias("candle_dragonfly"),
                    _ptc.gravestone(open_, high, low, close)
                    .cast(pl.Float64)
                    .alias("candle_gravestone"),
                    (
                        (prev_close < prev_open)
                        & (close > open_)
                        & (curr_body_low <= prev_body_low)
                        & (curr_body_high >= prev_body_high)
                    )
                    .cast(pl.Float64)
                    .alias("candle_bullish_engulfing"),
                    (
                        (prev_close > prev_open)
                        & (close < open_)
                        & (curr_body_low <= prev_body_low)
                        & (curr_body_high >= prev_body_high)
                    )
                    .cast(pl.Float64)
                    .alias("candle_bearish_engulfing"),
                ]
            )
        except DEFENSIVE_EXC:
            pass

    return df.with_columns(_pure_polars_candle_exprs())


__all__ = ["add_candle_pattern_columns"]
