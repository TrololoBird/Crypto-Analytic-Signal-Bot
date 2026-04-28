from __future__ import annotations

from typing import Any, cast

import numpy as np
import polars as pl

from .features_shared import (
    REQUIRED_OHLCV_COLUMNS,
    atr_from_true_range,
    clean_non_finite,
    ensure_columns,
    materialize_series,
    true_range,
)

__all__ = [
    "add_core_features",
    "adx",
    "atr",
    "ema",
    "realized_volatility",
    "roc",
    "rsi",
    "safe_close_position",
    "vwap",
]


def ema(df: pl.DataFrame, period: int, *, plta: Any = None, has_talib: bool = False) -> pl.Series:
    """Contract: input requires `close`; output Float64 series named `ema{period}`."""
    ensure_columns(df, ("close",), fn_name="ema")
    if has_talib:
        return materialize_series(
            cast(Any, plta).EMA(pl.col("close"), timeperiod=float(period)),
            df=df,
            name=f"ema{period}",
        )
    return materialize_series(
        df["close"].ewm_mean(span=period, adjust=False),
        df=df,
        name=f"ema{period}",
    )


def rsi(df: pl.DataFrame, period: int = 14, *, plta: Any = None, has_talib: bool = False) -> pl.Series:
    """Contract: input requires `close`; output RSI in [0,100] with neutral fill 50."""
    ensure_columns(df, ("close",), fn_name="rsi")
    if has_talib:
        return materialize_series(
            cast(Any, plta).RSI(pl.col("close"), timeperiod=float(period)),
            df=df,
            name=f"rsi{period}",
        )
    close = df["close"]
    delta = close.diff()
    gains = delta.clip(lower_bound=0.0)
    losses = (-delta).clip(lower_bound=0.0)
    avg_gain = materialize_series(gains.ewm_mean(alpha=1.0 / period, adjust=False), df=df, name="avg_gain")
    avg_loss = materialize_series(losses.ewm_mean(alpha=1.0 / period, adjust=False), df=df, name="avg_loss")
    rs = avg_gain / avg_loss
    raw = (100.0 - (100.0 / (1.0 + rs))).fill_nan(50.0)
    values: list[float] = []
    for gain, loss, current in zip(avg_gain.to_list(), avg_loss.to_list(), raw.to_list(), strict=False):
        g = float(gain or 0.0)
        l = float(loss or 0.0)
        if l == 0.0 and g > 0.0:
            values.append(100.0)
        elif g == 0.0 and l > 0.0:
            values.append(0.0)
        elif g == 0.0 and l == 0.0:
            values.append(50.0)
        else:
            values.append(float(current or 50.0))
    return pl.Series(f"rsi{period}", values, dtype=pl.Float64)


def atr(df: pl.DataFrame, period: int = 14, *, plta: Any = None, has_talib: bool = False) -> pl.Series:
    """Contract: input requires OHLC; output ATR series named `atr{period}`."""
    ensure_columns(df, REQUIRED_OHLCV_COLUMNS, fn_name="atr")
    if has_talib:
        return materialize_series(
            cast(Any, plta).ATR(pl.col("high"), pl.col("low"), pl.col("close"), timeperiod=float(period)),
            df=df,
            name=f"atr{period}",
        )
    return atr_from_true_range(true_range(df), period=period, df=df, name=f"atr{period}")


def adx(df: pl.DataFrame, period: int = 14, *, plta: Any = None, has_talib: bool = False) -> pl.Series:
    """Contract: input requires OHLC; output non-null/non-inf ADX series named `adx{period}`."""
    ensure_columns(df, REQUIRED_OHLCV_COLUMNS, fn_name="adx")
    if has_talib:
        return materialize_series(
            cast(Any, plta).ADX(pl.col("high"), pl.col("low"), pl.col("close"), timeperiod=float(period)),
            df=df,
            name=f"adx{period}",
        )
    high = df["high"]
    low = df["low"]
    up_move = high.diff()
    down_move = -low.diff()
    plus_dm = materialize_series(pl.when((up_move > down_move) & (up_move > 0.0)).then(up_move).otherwise(0.0), df=df, name="plus_dm")
    minus_dm = materialize_series(pl.when((down_move > up_move) & (down_move > 0.0)).then(down_move).otherwise(0.0), df=df, name="minus_dm")
    atr_series = atr(df, period, plta=plta, has_talib=has_talib)
    atr_safe = clean_non_finite(atr_series, fill=1e-9).replace(0.0, 1e-9)
    plus_di = 100.0 * plus_dm.ewm_mean(alpha=1.0 / period, adjust=False) / atr_safe
    minus_di = 100.0 * minus_dm.ewm_mean(alpha=1.0 / period, adjust=False) / atr_safe
    dx = 100.0 * (plus_di - minus_di).abs() / (plus_di + minus_di)
    return materialize_series(clean_non_finite(dx.ewm_mean(alpha=1.0 / period, adjust=False), fill=0.0), df=df, name=f"adx{period}")


def vwap(df: pl.DataFrame) -> pl.Series:
    """Contract: input requires OHLCV; output shifted cumulative VWAP named `vwap`."""
    ensure_columns(df, REQUIRED_OHLCV_COLUMNS, fn_name="vwap")
    typical = (df["high"] + df["low"] + df["close"]) / 3.0
    pv = typical * df["volume"]
    return materialize_series((pv.cum_sum().shift(1) / df["volume"].cum_sum().shift(1)).forward_fill(), df=df, name="vwap")


def roc(df: pl.DataFrame, period: int = 10, *, plta: Any = None, has_talib: bool = False) -> pl.Series:
    """Contract: input requires `close`; output percent ROC named `roc{period}`."""
    ensure_columns(df, ("close",), fn_name="roc")
    if has_talib:
        return materialize_series(
            cast(Any, plta).ROC(pl.col("close"), timeperiod=float(period)),
            df=df,
            name=f"roc{period}",
        )
    prev_close = df["close"].shift(period)
    return ((((df["close"] / prev_close) - 1.0) * 100.0).fill_nan(0.0).rename(f"roc{period}"))


def safe_close_position(df: pl.DataFrame, window: int = 20) -> pl.Series:
    """Contract: input requires HLC; output clipped [0,1] series named `close_position`."""
    ensure_columns(df, ("high", "low", "close"), fn_name="safe_close_position")
    low = df["low"].rolling_min(window_size=window)
    high = df["high"].rolling_max(window_size=window)
    return clean_non_finite((df["close"] - low) / (high - low), fill=0.5).clip(0.0, 1.0).rename("close_position")


def realized_volatility(df: pl.DataFrame, period: int = 20) -> pl.Series:
    """Contract: input requires `close`; output annualized-like rolling realized vol in %."""
    ensure_columns(df, ("close",), fn_name="realized_volatility")
    log_returns = df["close"].log() - df["close"].shift(1).log()
    return ((log_returns.rolling_std(window_size=period) * np.sqrt(period) * 100.0).fill_nan(0.0).rename(f"realized_vol_{period}"))


def add_core_features(df: pl.DataFrame, *, plta: Any = None, has_talib: bool = False) -> pl.DataFrame:
    """Contract: input requires OHLCV (+ optional taker_buy_base_volume); output adds core trend/vol columns."""
    work = df.with_columns([
        ema(df, 20, plta=plta, has_talib=has_talib).alias("ema20"),
        ema(df, 50, plta=plta, has_talib=has_talib).alias("ema50"),
        ema(df, 200, plta=plta, has_talib=has_talib).alias("ema200"),
        rsi(df, 14, plta=plta, has_talib=has_talib).alias("rsi14"),
        adx(df, 14, plta=plta, has_talib=has_talib).alias("adx14"),
        atr(df, 14, plta=plta, has_talib=has_talib).alias("atr14"),
    ])
    macd_line = ema(work, 12, plta=plta, has_talib=has_talib) - ema(work, 26, plta=plta, has_talib=has_talib)
    work = work.with_columns([macd_line.alias("macd_line")])
    macd_signal = work["macd_line"].ewm_mean(span=9, adjust=False)
    work = work.with_columns([
        macd_signal.alias("macd_signal"),
        (pl.col("macd_line") - macd_signal).alias("macd_hist"),
        pl.col("low").rolling_min(window_size=20).alias("donchian_low20"),
        pl.col("high").rolling_max(window_size=20).alias("donchian_high20"),
        pl.col("volume").rolling_mean(window_size=20).alias("volume_mean20"),
    ])
    work = work.with_columns([
        pl.col("donchian_low20").shift(1).alias("prev_donchian_low20"),
        pl.col("donchian_high20").shift(1).alias("prev_donchian_high20"),
        (pl.col("volume") / pl.col("volume_mean20")).alias("volume_ratio20"),
        vwap(work).alias("vwap"),
    ])
    price_dev_sq = (work["close"] - work["vwap"]) ** 2
    vwap_std = (price_dev_sq.cum_sum() / pl.Series("n", range(1, work.height + 1), dtype=pl.Float64)).sqrt() if work.height else price_dev_sq
    work = work.with_columns([
        vwap_std.alias("vwap_std"),
        (pl.col("vwap") + vwap_std).alias("vwap_upper1"),
        (pl.col("vwap") - vwap_std).alias("vwap_lower1"),
        (pl.col("vwap") + 2.0 * vwap_std).alias("vwap_upper2"),
        (pl.col("vwap") - 2.0 * vwap_std).alias("vwap_lower2"),
        (((pl.col("close") - pl.col("vwap")) / pl.col("vwap")) * 100.0).fill_nan(0.0).alias("vwap_deviation_pct"),
        ((pl.col("atr14") / pl.col("close")) * 100.0).clip(lower_bound=0.001).alias("atr_pct"),
        safe_close_position(work, window=20).alias("close_position"),
    ])
    if "taker_buy_base_volume" in work.columns:
        work = work.with_columns([((pl.col("taker_buy_base_volume") / pl.col("volume")).rolling_mean(window_size=5).clip(0.0, 1.0).alias("delta_ratio"))])
    else:
        work = work.with_columns([pl.lit(0.5).alias("delta_ratio")])
    return work
