"""Low-level manipulation primitives — Polars-first, zero Python loops for TA.

All functions accept ``pl.DataFrame`` with columns ``[ts, open, high, low, close, volume]``.
Feature computations use Polars expressions + polars_ta — no manual TA.
"""
from __future__ import annotations

from typing import Any

import polars as pl
import polars_ta.ta as plta


def ohlcv_to_df(ohlcv: list[list[float]]) -> pl.DataFrame:
    return pl.DataFrame({
        "ts": [float(r[0]) for r in ohlcv],
        "open": [float(r[1]) for r in ohlcv],
        "high": [float(r[2]) for r in ohlcv],
        "low": [float(r[3]) for r in ohlcv],
        "close": [float(r[4]) for r in ohlcv],
        "volume": [float(r[5]) for r in ohlcv],
    })


def _pl_features() -> list[pl.Expr]:
    return [
        (pl.col("close") - pl.col("open")).abs().alias("_body"),
        (pl.when(pl.col("open") > 0)
         .then((pl.col("close") - pl.col("open")).abs() / pl.col("open") * 100)
         .otherwise(0.0)).alias("_body_pct"),
        (pl.col("high") - pl.col("low")).alias("_range"),
    ]


def _swing_exprs() -> list[pl.Expr]:
    return [
        ((pl.col("high") > pl.col("high").shift(1)) &
         (pl.col("high") > pl.col("high").shift(2)) &
         (pl.col("high") >= pl.col("high").shift(-1)) &
         (pl.col("high") >= pl.col("high").shift(-2))).alias("_swing_high"),
        ((pl.col("low") < pl.col("low").shift(1)) &
         (pl.col("low") < pl.col("low").shift(2)) &
         (pl.col("low") <= pl.col("low").shift(-1)) &
         (pl.col("low") <= pl.col("low").shift(-2))).alias("_swing_low"),
    ]


def compute_features(df: pl.DataFrame) -> pl.DataFrame:
    """Augment DataFrame with computed columns. Mutate-free (returns new)."""
    return df.with_columns(_pl_features() + _swing_exprs())


def _resolve_scalar(df: pl.DataFrame, expr: pl.Expr) -> float:
    """Materialize a Polars expression to a scalar float (last non-null value)."""
    result = df.select(expr)
    col = result.get_column(result.columns[0])
    last_ = col.drop_nulls().last()
    return float(last_) if last_ is not None else 0.0


def atr(df: pl.DataFrame, period: int = 14) -> float:
    """ATR via polars_ta.ATR — materialized to scalar."""
    return _resolve_scalar(
        df,
        plta.ATR(pl.col("high"), pl.col("low"), pl.col("close"), timeperiod=period),
    )


def atr_pct(df: pl.DataFrame, period: int = 14) -> pl.Series:
    """ATR as % of open — per-bar, resolved against DataFrame. Zero-open bars → 0."""
    expr = (pl.when(pl.col("open") > 0)
            .then(plta.ATR(pl.col("high"), pl.col("low"), pl.col("close"), timeperiod=period)
                  / pl.col("open") * 100)
            .otherwise(0.0).alias("_atr_pct"))
    return df.select(expr).get_column("_atr_pct")


def detect_impulse(
    df: pl.DataFrame, *, lookback: int = 30, direction: str | None = None,
) -> tuple[bool, int | None]:
    """Impulse = candle body ≥ 1.5× ATR% within lookback window.
    
    Scans the last ``lookback`` complete bars (not the current forming bar).
    If ``direction`` is ''up'', only green candles qualify; ''down'' → only red.
    Per-bar normalization: threshold = 1.5 × ATR(14) / open × 100.
    """
    if len(df) < 20:
        return False, None
    df_c = compute_features(df)
    atr_val = atr(df_c, 14)
    if atr_val <= 0:
        return False, None
    threshold = 1.5 * atr_val / df_c["open"] * 100.0
    body_ok = df_c["_body_pct"] >= threshold
    if direction == "up":
        dir_ok = df_c["close"] > df_c["open"]
    elif direction == "down":
        dir_ok = df_c["close"] < df_c["open"]
    else:
        dir_ok = pl.Series([True] * len(df_c))
    impulse = body_ok & dir_ok
    start = max(0, len(df_c) - lookback - 1)
    for idx in range(len(df_c) - 2, start - 1, -1):
        if impulse[idx]:
            return True, int(idx)
    return False, None


def detect_consecutive_impulse(df: pl.DataFrame, min_count: int = 3) -> tuple[bool, int | None]:
    """≥min_count consecutive same-direction candles with body ≥ noise floor."""
    if len(df) < min_count + 10:
        return False, None
    df_c = compute_features(df)
    avg_body = float(df_c["_body_pct"].tail(30).mean())
    if avg_body <= 0:
        return False, None
    above_noise = df_c["_body_pct"] >= avg_body * 0.8
    direction = (df_c["close"] > df_c["open"]).cast(pl.Int8)
    count = 0
    start_idx = len(df_c) - 1
    dir_val: int | None = None
    for i in range(min(len(df_c), 12)):
        idx = len(df_c) - 1 - i
        if not above_noise[idx]:
            break
        d = int(direction[idx])
        if dir_val is None:
            dir_val = d
        elif d != dir_val:
            break
        count += 1
        start_idx = idx
    if count >= min_count:
        return True, int(start_idx)
    return False, None


def detect_absorption(df: pl.DataFrame, impulse_idx: int, *, absorb_pct: float = 0.80) -> bool:
    """Price EVER retraced ≥ absorb_pct of the impulse range since impulse_idx.
    
    Uses the EXTREME opposite price reached after the impulse (not latest close),
    because absorption is a past event — once detected, it stays detected even if
    price later reverses back toward the impulse extreme.
    """
    if impulse_idx < 1 or impulse_idx >= len(df):
        return False
    pre = float(df["close"][impulse_idx - 1])
    imp_open = float(df["open"][impulse_idx])
    imp_close = float(df["close"][impulse_idx])
    is_green = imp_close > imp_open
    extreme = float(df["high"][impulse_idx]) if is_green else float(df["low"][impulse_idx])
    imp_range = abs(extreme - pre)
    if imp_range <= 0:
        return False
    post = df.slice(impulse_idx)
    if is_green:
        retrace_high = float(post["low"].min())  # opposite: price went down
    else:
        retrace_high = float(post["high"].max())
    return abs(retrace_high - extreme) >= imp_range * absorb_pct


def detect_one_candle_absorption(df: pl.DataFrame, impulse_range_pct: float) -> bool:
    """Single candle body ≥ 60 % of impulse range among last 5 bars."""
    body_pct = (df["close"] - df["open"]).abs() / df["open"] * 100
    for i in range(1, min(5, len(df))):
        if float(body_pct[-i]) >= impulse_range_pct * 0.60:
            return True
    return False


def detect_bokovik(df: pl.DataFrame, *, window: int = 30, min_touches: int = 3, max_width_pct: float = 15.0, start_idx: int | None = None) -> dict[str, Any] | None:
    """Sideways range with ATR compression — all stats via Polars.
    
    If ``start_idx`` is set, the window starts from that index (post-event),
    rather than from the tail — avoids including the impulse/absorption candles
    that would inflate the range.
    """
    if len(df) < window * 2:
        return None
    if start_idx is not None:
        recent = df.slice(start_idx, window)
    else:
        recent = df.tail(window)
    lo = float(recent["low"].min())
    hi = float(recent["high"].max())
    mid = (lo + hi) / 2.0
    width_pct = (hi - lo) / mid * 100.0 if mid > 0 else 0.0
    if width_pct < 1.0 or width_pct > max_width_pct:
        return None
    touch_buf = width_pct * 0.05 / 100.0 * mid
    touches_lo = int(((recent["low"] - lo).abs() <= touch_buf).sum())
    touches_hi = int(((recent["high"] - hi).abs() <= touch_buf).sum())
    touches = touches_lo + touches_hi
    if touches < min_touches:
        return None
    if start_idx is not None:
        prior = df.slice(max(0, start_idx - window), window)
    else:
        prior = df.slice(len(df) - window * 2, window)
    current_atr = atr(recent, 14)
    prior_atr = atr(prior, 14)
    atr_ratio = current_atr / prior_atr if prior_atr > 0 else 1.0
    if atr_ratio > 0.70:
        return None
    return {
        "lo": lo, "hi": hi, "mid": mid, "width_pct": round(width_pct, 2),
        "touches": touches, "atr_ratio": round(atr_ratio, 3),
    }


def _sweep_check(df: pl.DataFrame, level: float, *, side: str, wick_ratio: float = 0.30) -> tuple[bool, float, float]:
    """Unified sweep check. side='low' → sweep below level; 'high' → sweep above."""
    if side == "low":
        candidates = df.filter(pl.col("low") < level)
    else:
        candidates = df.filter(pl.col("high") > level)
    if candidates.is_empty():
        return False, 0.0, 0.0
    for row in reversed(candidates.to_dicts()):
        rng = row["high"] - row["low"]
        if rng <= 0:
            continue
        if side == "low":
            lower_wick = min(row["close"], row["open"]) - row["low"]
            if lower_wick / rng >= wick_ratio and row["close"] >= level:
                return True, row["low"], row["ts"]
        else:
            upper_wick = row["high"] - max(row["close"], row["open"])
            if upper_wick / rng >= wick_ratio and row["close"] <= level:
                return True, row["high"], row["ts"]
    return False, 0.0, 0.0


def detect_sweep_low(df: pl.DataFrame, level: float, *, wick_ratio: float = 0.30) -> tuple[bool, float, float]:
    return _sweep_check(df, level, side="low", wick_ratio=wick_ratio)


def detect_sweep_high(df: pl.DataFrame, level: float, *, wick_ratio: float = 0.30) -> tuple[bool, float, float]:
    return _sweep_check(df, level, side="high", wick_ratio=wick_ratio)


def candle_fade_ratio(df: pl.DataFrame, n: int = 8, *, peak_high: float | None = None) -> tuple[float, float]:
    """Body / range fade ratio.
    
    If peak_high is given, finds the peak bar and uses its body vs prior avg
    (checks for single-candle exhaustion at the pump top, not the dump bars).
    """
    if peak_high is not None:
        peak_series: pl.Series = (df["high"] - peak_high).abs()
        peak_idx = int(peak_series.arg_min()) if len(peak_series) > 0 else -1
        if 2 <= peak_idx < len(df) - 1:
            df_c = compute_features(df)
            peak_body = float(df_c["_body_pct"][peak_idx])
            pre_avg = float(df_c["_body_pct"].slice(max(0, peak_idx - 4), 4).mean())
            body_r = peak_body / pre_avg if pre_avg > 0 else 1.0
            return min(body_r, 2.0), min(body_r, 2.0)
    if len(df) < n * 2 + 1:
        return 1.0, 1.0
    df_c = compute_features(df)
    avg_body_rec = float(df_c["_body_pct"].tail(n).mean())
    avg_body_pri = float(df_c["_body_pct"].slice(len(df_c) - n * 2, n).mean()) if len(df_c) >= n * 2 else 0.0
    avg_range_rec = float(df_c["_range"].tail(n).mean())
    avg_range_pri = float(df_c["_range"].slice(len(df_c) - n * 2, n).mean()) if len(df_c) >= n * 2 else 0.0
    body_ratio = avg_body_rec / avg_body_pri if avg_body_pri > 0 else 1.0
    range_ratio = avg_range_rec / avg_range_pri if avg_range_pri > 0 else 1.0
    return body_ratio, range_ratio


def _swing_value_mask(df: pl.DataFrame, side: str) -> pl.Series:
    """Return Series of swing high/low values (NaN where not a swing point)."""
    df_c = compute_features(df)
    col = "high" if side == "high" else "low"
    swing_col = "_swing_high" if side == "high" else "_swing_low"
    return df_c.select(pl.col(col).filter(pl.col(swing_col))).to_series()


def bos_up(df: pl.DataFrame, *, buffer: float = 0.003) -> bool:
    """Break of structure up — close above the most recent swing high."""
    df_c = compute_features(df)
    swing_vals = df_c.filter(pl.col("_swing_high"))["high"]
    if swing_vals.is_empty():
        return False
    sh_val = float(swing_vals.tail(1)[0])
    return float(df_c["close"][-1]) > sh_val * (1.0 + buffer) and float(df_c["close"][-2]) <= sh_val


def rejection_at_peak(df: pl.DataFrame, peak_high: float) -> bool:
    """Instant rejection at pump peak: 1-bar reversal (pump → dump in same bar).
    
    Criteria:
    1. Peak bar's close < open (red candle / rejection)
    2. Peak bar's close < previous bar's close
    3. Peak bar's range > 1.5x avg range of prior 3 bars
    """
    peak_series: pl.Series = (df["high"] - peak_high).abs()
    peak_idx = int(peak_series.arg_min())
    if peak_idx < 3 or peak_idx >= len(df):
        return False
    df_c = compute_features(df)
    c_peak = float(df_c["close"][peak_idx])
    o_peak = float(df_c["open"][peak_idx])
    if c_peak >= o_peak:
        return False  # not a rejection candle (close >= open)
    if peak_idx == 0:
        return False
    c_prev = float(df_c["close"][peak_idx - 1])
    if c_peak >= c_prev:
        return False  # close didn't drop below prior close
    rng_peak = float(df_c["_range"][peak_idx])
    rng_avg = float(df_c["_range"].slice(peak_idx - 3, 3).mean())
    return rng_peak > rng_avg * 1.5 if rng_avg > 0 else False


def bos_down(df: pl.DataFrame, *, buffer: float = 0.003) -> bool:
    """Break of structure down — ANY swing low broken by a later close in the dataset.
    
    Works post-factum too: even if price recovered above the most recent
    swing low, we check if any prior swing low was violated.
    """
    df_c = compute_features(df)
    swing_vals = df_c.filter(pl.col("_swing_low"))
    if swing_vals.is_empty():
        return False
    close_min = float(df_c["close"].min())
    for row in swing_vals.iter_rows(named=True):
        sl = float(row["low"])
        if close_min < sl * (1.0 - buffer):
            return True
    return False


def choch_bull(df: pl.DataFrame, *, buffer: float = 0.003) -> bool:
    """Change of character bullish — close above the last lower high."""
    df_c = compute_features(df)
    swing_vals = df_c.filter(pl.col("_swing_high"))["high"]
    if len(swing_vals) < 2:
        return False
    last_sh = float(swing_vals.tail(1)[0])
    prev_sh = float(swing_vals.tail(2)[0])
    if last_sh >= prev_sh:
        return False
    return float(df_c["close"][-1]) > last_sh * (1.0 + buffer) and float(df_c["close"][-2]) <= last_sh


def choch_bear(df: pl.DataFrame, *, buffer: float = 0.003) -> bool:
    """Change of character bearish — close below any recent higher low."""
    df_c = compute_features(df)
    swing_lows = df_c.filter(pl.col("_swing_low"))["low"]
    if len(swing_lows) < 2:
        return False
    close_min = float(df_c["close"].min())
    for i in range(len(swing_lows) - 1):
        if float(swing_lows[i + 1]) > float(swing_lows[i]):
            if close_min < float(swing_lows[i + 1]) * (1.0 - buffer):
                return True
    return False


def no_liquidity_above(df: pl.DataFrame, current_price: float, *, pump_high: float | None = None) -> bool:
    """No near-term swing highs above the pump peak (liquidity exhausted by the sweep).
    
    The pump blew through all intermediate levels; only check if there are
    swing highs still standing above the pump peak within the lookback window.
    """
    df_c = compute_features(df)
    peak = pump_high if pump_high is not None else current_price
    above = df_c.filter(df_c["_swing_high"] & (df_c["high"] > peak * 1.005))
    return above.height <= 1


def no_liquidity_below(df: pl.DataFrame, current_price: float, *, pump_low: float | None = None) -> bool:
    """No near-term swing lows below the pump trough (liquidity exhausted by the sweep)."""
    df_c = compute_features(df)
    trough = pump_low if pump_low is not None else current_price
    below = df_c.filter(df_c["_swing_low"] & (df_c["low"] < trough * 0.995))
    return below.height <= 1

