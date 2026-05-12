from __future__ import annotations

from typing import Any, cast

import numpy as np
import polars as pl

from .features_oscillators import add_oscillator_features
from .features_shared import (
    atr_from_true_range,
    clean_non_finite,
    finite_float,
    materialize_series,
    true_range,
    wilder_mean,
)
from .features_structure import hull_moving_average, ichimoku_lines

__all__ = ["add_advanced_indicators", "supertrend"]


def supertrend(
    df: pl.DataFrame, period: int = 10, multiplier: float = 3.0
) -> tuple[pl.Series, pl.Series]:
    """Contract: input requires HLC; output (`supertrend`,`supertrend_dir`), dir ∈ {-1,1}."""
    close = df["close"]
    atr = atr_from_true_range(true_range(df), period=period, df=df, name="supertrend_atr")
    hl2 = (df["high"] + df["low"]) / 2.0
    basic_upper = hl2 + multiplier * atr
    basic_lower = hl2 - multiplier * atr
    close_vals = close.to_list()
    upper_vals = basic_upper.to_list()
    lower_vals = basic_lower.to_list()
    size = len(close_vals)
    if size == 0:
        empty = pl.Series("supertrend", [], dtype=pl.Float64)
        return empty, pl.Series("supertrend_dir", [], dtype=pl.Float64)
    final_upper = [float(upper_vals[0])]
    final_lower = [float(lower_vals[0])]
    st = [float(upper_vals[0])]
    for idx in range(1, size):
        bu = float(upper_vals[idx])
        bl = float(lower_vals[idx])
        prev_close = float(close_vals[idx - 1])
        prev_fu = final_upper[idx - 1]
        prev_fl = final_lower[idx - 1]
        fu = bu if (bu < prev_fu or prev_close > prev_fu) else prev_fu
        fl = bl if (bl > prev_fl or prev_close < prev_fl) else prev_fl
        final_upper.append(fu)
        final_lower.append(fl)
        prev_st = st[idx - 1]
        curr_close = float(close_vals[idx])
        if prev_st == prev_fu:
            next_st = fu if curr_close <= fu else fl
        else:
            next_st = fl if curr_close >= fl else fu
        st.append(next_st)
    return pl.Series("supertrend", st, dtype=pl.Float64), pl.Series(
        "supertrend_dir",
        [1.0 if float(c) >= float(s) else -1.0 for c, s in zip(close_vals, st, strict=False)],
        dtype=pl.Float64,
    )


def _bollinger_bands(
    close: pl.Series, period: int = 20, nbdev: float = 2.0
) -> tuple[pl.Series, pl.Series, pl.Series]:
    middle = close.rolling_mean(window_size=period)
    std = close.rolling_std(window_size=period, ddof=1)
    return middle + nbdev * std, middle, middle - nbdev * std


def _keltner_channels(
    df: pl.DataFrame, period: int = 20, multiplier: float = 2.0, atr_period: int = 10
) -> tuple[pl.Series, pl.Series, pl.Series]:
    typical = (df["high"] + df["low"] + df["close"]) / 3.0
    middle = typical.ewm_mean(span=period, adjust=False).rename("kc_middle")
    atr = materialize_series(
        wilder_mean(true_range(df), period=atr_period, name="kc_atr"),
        df=df,
        name="kc_atr",
    )
    return middle + multiplier * atr, middle, middle - multiplier * atr


def _parabolic_sar(
    df: pl.DataFrame, *, step: float = 0.02, max_step: float = 0.2
) -> tuple[pl.Series, pl.Series, pl.Series]:
    high_vals = [float(v or 0.0) for v in df["high"]]
    low_vals = [float(v or 0.0) for v in df["low"]]
    close_vals = [float(v or 0.0) for v in df["close"]]
    size = len(close_vals)
    if size == 0:
        e = pl.Series("psar_long", [], dtype=pl.Float64)
        return (
            e,
            pl.Series("psar_short", [], dtype=pl.Float64),
            pl.Series("psar_reversal", [], dtype=pl.Float64),
        )
    long_psar: list[float | None] = [None] * size
    short_psar: list[float | None] = [None] * size
    rev = [0.0] * size
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
                rev[i] = -1.0
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
                rev[i] = 1.0
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
        pl.Series("psar_reversal", rev, dtype=pl.Float64),
    )


def _aroon(df: pl.DataFrame, period: int = 14) -> tuple[pl.Series, pl.Series, pl.Series]:
    """Aroon indicator - vectorized via rolling_map."""
    high = df["high"]
    low = df["low"]

    def bars_since_argmax(s: pl.Series) -> int:
        return len(s) - 1 - cast(int, s.arg_max())

    def bars_since_argmin(s: pl.Series) -> int:
        return len(s) - 1 - cast(int, s.arg_min())

    up_days = high.rolling_map(bars_since_argmax, window_size=period + 1)
    down_days = low.rolling_map(bars_since_argmin, window_size=period + 1)

    up_s = (period - up_days) / period * 100.0
    down_s = (period - down_days) / period * 100.0

    return (
        materialize_series(up_s, df=df, name=f"aroon_up{period}"),
        materialize_series(down_s, df=df, name=f"aroon_down{period}"),
        materialize_series(up_s - down_s, df=df, name=f"aroon_osc{period}"),
    )


def _fisher_transform(df: pl.DataFrame, period: int = 10) -> tuple[pl.Series, pl.Series]:
    high = [float(v or 0.0) for v in df["high"]]
    low = [float(v or 0.0) for v in df["low"]]
    close = [float(v or 0.0) for v in df["close"]]
    values = [0.0] * len(close)
    fisher = [0.0] * len(close)
    for i in range(len(close)):
        start = max(0, i - period + 1)
        hh = max(high[start : i + 1])
        ll = min(low[start : i + 1])
        width = max(hh - ll, 1e-9)
        raw = 2.0 * (((close[i] - ll) / width) - 0.5)
        prev_v = values[i - 1] if i > 0 else 0.0
        v = max(min(0.33 * raw + 0.67 * prev_v, 0.999), -0.999)
        values[i] = v
        fisher[i] = 0.5 * np.log((1.0 + v) / (1.0 - v)) + 0.5 * (fisher[i - 1] if i > 0 else 0.0)
    fs = pl.Series("fisher", fisher, dtype=pl.Float64)
    return fs, fs.ewm_mean(span=5, adjust=False).rename("fisher_signal")


def _squeeze_momentum(
    df: pl.DataFrame, period: int = 20
) -> tuple[pl.Series, pl.Series, pl.Series, pl.Series]:
    bb_upper, bb_mid, bb_lower = _bollinger_bands(df["close"], period=period, nbdev=2.0)
    kc_upper, _, kc_lower = _keltner_channels(df, period=period, multiplier=1.5)
    on = ((bb_lower > kc_lower) & (bb_upper < kc_upper)).cast(pl.Float64).rename("squeeze_on")
    off = ((bb_lower < kc_lower) & (bb_upper > kc_upper)).cast(pl.Float64).rename("squeeze_off")
    no = pl.Series(
        "squeeze_no",
        [
            max(0.0, min(1.0, 1.0 - max(float(a or 0.0), float(b or 0.0))))
            for a, b in zip(on, off, strict=False)
        ],
        dtype=pl.Float64,
    )
    basis = (
        (
            (df["high"].rolling_max(window_size=period) + df["low"].rolling_min(window_size=period))
            / 2.0
        )
        + bb_mid
    ) / 2.0
    hist = clean_non_finite((df["close"] - basis).ewm_mean(span=5, adjust=False), fill=0.0).rename(
        "squeeze_hist"
    )
    return hist, on, off, no


def _chandelier_exit(
    df: pl.DataFrame, period: int = 22, atr_mult: float = 3.0, *, atr_fn: Any = None
) -> tuple[pl.Series, pl.Series, pl.Series]:
    atr = (
        atr_fn(df, period)
        if atr_fn
        else atr_from_true_range(true_range(df), period=period, df=df, name=f"atr{period}")
    )
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
        materialize_series(direction, df=df, name="chandelier_dir"),
    )


def _volume_profile(df: pl.DataFrame, bins: int = 12) -> pl.Expr:
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

    raw_price_min = v_prices.min()
    raw_price_max = v_prices.max()
    price_min = None if raw_price_min is None else finite_float(raw_price_min)
    price_max = None if raw_price_max is None else finite_float(raw_price_max)

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


def add_advanced_indicators(
    df: pl.DataFrame,
    *,
    plta: Any,
    has_talib: bool,
    atr_fn: Any,
    roc_fn: Any,
    log_fallback: Any,
) -> pl.DataFrame:
    """Contract: input requires core OHLCV+`close`; output adds advanced + oscillator columns used by strategies."""
    result = df
    st, st_dir = supertrend(df)
    result = result.with_columns([st.alias("supertrend"), st_dir.alias("supertrend_dir")])
    try:
        if has_talib:
            obv = materialize_series(plta.OBV(pl.col("close"), pl.col("volume")), df=df, name="obv")
        else:
            close_diff = df["close"].diff()
            direction = pl.Series(
                "obv_direction",
                [
                    1.0 if float(delta or 0.0) > 0.0 else -1.0 if float(delta or 0.0) < 0.0 else 0.0
                    for delta in close_diff
                ],
                dtype=pl.Float64,
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
        log_fallback("obv", exc)
        result = result.with_columns(
            [
                pl.lit(0.0).alias("obv"),
                pl.lit(0.0).alias("obv_ema20"),
                pl.lit(0.0).alias("obv_above_ema"),
            ]
        )

    upper, middle, lower = _bollinger_bands(df["close"], period=20, nbdev=2.0)
    result = result.with_columns(
        [
            clean_non_finite((df["close"] - lower) / (upper - lower), fill=0.5).alias("bb_pct_b"),
            clean_non_finite((upper - lower) / middle * 100.0, fill=0.0).alias("bb_width"),
        ]
    )
    kc_upper, _, kc_lower = _keltner_channels(df, period=20, multiplier=2.0)
    result = result.with_columns(
        [
            kc_upper.alias("kc_upper"),
            kc_lower.alias("kc_lower"),
            ((kc_upper - kc_lower) / df["close"]).fill_nan(0.04).alias("kc_width"),
        ]
    )
    result = result.with_columns(
        [
            hull_moving_average(df["close"], 9, name="hma9"),
            hull_moving_average(df["close"], 21, name="hma21"),
        ]
    )
    psar_long, psar_short, psar_reversal = _parabolic_sar(df)
    result = result.with_columns([psar_long, psar_short, psar_reversal])
    aroon_up, aroon_down, aroon_osc = _aroon(df)
    result = result.with_columns([aroon_up, aroon_down, aroon_osc])
    result = add_oscillator_features(result, plta=plta, has_talib=has_talib)
    fisher, fisher_signal = _fisher_transform(df)
    squeeze_hist, squeeze_on, squeeze_off, squeeze_no = _squeeze_momentum(df)
    chand_l, chand_s, chand_d = _chandelier_exit(df, atr_fn=atr_fn)
    tenkan, kijun, senkou_a, senkou_b = ichimoku_lines(result)
    result = result.with_columns(
        [
            fisher,
            fisher_signal,
            squeeze_hist,
            squeeze_on,
            squeeze_off,
            squeeze_no,
            chand_l,
            chand_s,
            chand_d,
            _volume_profile(result, bins=12),
            (
                (df["close"] - df["close"].rolling_mean(window_size=30))
                / df["close"].rolling_std(window_size=30)
            )
            .fill_nan(0.0)
            .alias("zscore30"),
            roc_fn(df, 5).fill_nan(0.0).alias("slope5"),
            tenkan.alias("ichi_tenkan"),
            kijun.alias("ichi_kijun"),
            senkou_a.alias("ichi_senkou_a"),
            senkou_b.alias("ichi_senkou_b"),
        ]
    )
    return result
