from __future__ import annotations

from typing import Any, cast

import polars as pl

from .features_shared import clean_non_finite, ensure_columns, materialize_series

__all__ = [
    "add_oscillator_features",
    "cci",
    "cmf",
    "mfi",
    "stochastic",
    "ultimate_oscillator",
]


def stochastic(
    df: pl.DataFrame, period: int = 14, smooth_k: int = 3, smooth_d: int = 3
) -> tuple[pl.Series, pl.Series]:
    """Contract: input requires HLC; output (`stoch_k14`, `stoch_d14`) in [0,100] with safe fill."""
    ensure_columns(df, ("high", "low", "close"), fn_name="stochastic")
    low = df["low"].shift(1).rolling_min(window_size=period)
    high = df["high"].shift(1).rolling_max(window_size=period)
    raw_k = clean_non_finite(((df["close"] - low) / (high - low)) * 100.0, fill=50.0)
    k = clean_non_finite(raw_k.rolling_mean(window_size=smooth_k), fill=50.0).rename(
        "stoch_k14"
    )
    d = clean_non_finite(k.rolling_mean(window_size=smooth_d), fill=50.0).rename(
        "stoch_d14"
    )
    return k, d


def cci(
    df: pl.DataFrame, period: int = 20, *, plta: Any = None, has_talib: bool = False
) -> pl.Series:
    """Contract: input requires HLC; output CCI series named `cci{period}` with non-finite sanitized to 0."""
    ensure_columns(df, ("high", "low", "close"), fn_name="cci")
    if has_talib:
        return materialize_series(
            cast(Any, plta).CCI(
                pl.col("high"), pl.col("low"), pl.col("close"), timeperiod=float(period)
            ),
            df=df,
            name=f"cci{period}",
        )
    typical = (df["high"] + df["low"] + df["close"]) / 3.0
    sma = typical.rolling_mean(window_size=period)
    mean_dev = (typical - sma).abs().rolling_mean(window_size=period)
    return clean_non_finite((typical - sma) / (0.015 * mean_dev), fill=0.0).rename(
        f"cci{period}"
    )


def mfi(
    df: pl.DataFrame, period: int = 14, *, plta: Any = None, has_talib: bool = False
) -> pl.Series:
    """Contract: input requires OHLCV; output MFI series named `mfi{period}` with neutral fill 50."""
    ensure_columns(df, ("high", "low", "close", "volume"), fn_name="mfi")
    if has_talib:
        return materialize_series(
            cast(Any, plta).MFI(
                pl.col("high"),
                pl.col("low"),
                pl.col("close"),
                pl.col("volume"),
                timeperiod=float(period),
            ),
            df=df,
            name=f"mfi{period}",
        )
    typical = (df["high"] + df["low"] + df["close"]) / 3.0
    money_flow = typical * df["volume"]
    delta = typical.diff()
    pos = pl.Series(
        "pos",
        [
            float(mf or 0.0) if float(chg or 0.0) > 0.0 else 0.0
            for mf, chg in zip(money_flow, delta, strict=False)
        ],
        dtype=pl.Float64,
    )
    neg = pl.Series(
        "neg",
        [
            float(mf or 0.0) if float(chg or 0.0) < 0.0 else 0.0
            for mf, chg in zip(money_flow, delta, strict=False)
        ],
        dtype=pl.Float64,
    )
    vals: list[float] = []
    for p, n in zip(
        pos.rolling_sum(window_size=period),
        neg.rolling_sum(window_size=period),
        strict=False,
    ):
        pv = float(p or 0.0)
        nv = float(n or 0.0)
        if nv <= 0.0 and pv <= 0.0:
            vals.append(50.0)
        elif nv <= 0.0:
            vals.append(100.0)
        else:
            vals.append(100.0 - (100.0 / (1.0 + (pv / nv))))
    return pl.Series(f"mfi{period}", vals, dtype=pl.Float64)


def cmf(df: pl.DataFrame, period: int = 20) -> pl.Series:
    """Contract: input requires OHLCV; output CMF series named `cmf{period}` with NaN fill 0."""
    ensure_columns(df, ("high", "low", "close", "volume"), fn_name="cmf")
    multipliers: list[float] = []
    for high, low, close in zip(df["high"], df["low"], df["close"], strict=False):
        high_value = float(high or 0.0)
        low_value = float(low or 0.0)
        close_value = float(close or 0.0)
        width = high_value - low_value
        multipliers.append(
            0.0
            if width <= 0.0
            else ((close_value - low_value) - (high_value - close_value)) / width
        )
    mfv = pl.Series("mfm", multipliers, dtype=pl.Float64) * df["volume"]
    return (
        (
            mfv.rolling_sum(window_size=period)
            / df["volume"].rolling_sum(window_size=period)
        )
        .fill_nan(0.0)
        .rename(f"cmf{period}")
    )


def ultimate_oscillator(
    df: pl.DataFrame, p1: int = 7, p2: int = 14, p3: int = 28
) -> pl.Series:
    """Contract: input requires HLC; output `uo` series with neutral fill 50."""
    ensure_columns(df, ("high", "low", "close"), fn_name="ultimate_oscillator")
    prev_close = df["close"].shift(1)
    min_low = materialize_series(
        pl.min_horizontal(df["low"], prev_close), df=df, name="uo_min_low"
    )
    max_high = materialize_series(
        pl.max_horizontal(df["high"], prev_close), df=df, name="uo_max_high"
    )
    bp = df["close"] - min_low
    tr = max_high - min_low
    avg1 = bp.rolling_sum(window_size=p1) / tr.rolling_sum(window_size=p1)
    avg2 = bp.rolling_sum(window_size=p2) / tr.rolling_sum(window_size=p2)
    avg3 = bp.rolling_sum(window_size=p3) / tr.rolling_sum(window_size=p3)
    return clean_non_finite(
        (100.0 * ((4.0 * avg1) + (2.0 * avg2) + avg3) / 7.0).rename("uo"), fill=50.0
    )


def add_oscillator_features(
    df: pl.DataFrame, *, plta: Any = None, has_talib: bool = False
) -> pl.DataFrame:
    """Contract: input requires OHLCV; output adds oscillators: stoch*, cci20, mfi14, cmf20, uo, willr14."""
    stoch_k, stoch_d = stochastic(df)
    rolling_high = df["high"].rolling_max(window_size=14)
    rolling_low = df["low"].rolling_min(window_size=14)
    willr = (
        ((rolling_high - df["close"]) / (rolling_high - rolling_low)) * -100.0
    ).fill_nan(-50.0)
    return df.with_columns(
        [
            stoch_k.alias("stoch_k14"),
            stoch_d.alias("stoch_d14"),
            (stoch_k - stoch_d).fill_nan(0.0).alias("stoch_h14"),
            cci(df, 20, plta=plta, has_talib=has_talib).fill_nan(0.0).alias("cci20"),
            willr.alias("willr14"),
            mfi(df, 14, plta=plta, has_talib=has_talib).fill_nan(50.0).alias("mfi14"),
            cmf(df, 20).fill_nan(0.0).alias("cmf20"),
            ultimate_oscillator(df, 7, 14, 28).fill_nan(50.0).alias("uo"),
        ]
    )
