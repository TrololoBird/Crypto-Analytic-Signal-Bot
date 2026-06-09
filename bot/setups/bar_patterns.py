"""Shared candle pattern helpers (pin, engulf, overshoot reclaim)."""

from __future__ import annotations

import math


def pin_bar_confirm(
    direction: str,
    open_: float,
    high: float,
    low: float,
    close: float,
    *,
    min_wick_frac: float = 0.5,
) -> bool:
    bar_range = high - low
    if bar_range <= 0.0 or not all(math.isfinite(v) for v in (open_, high, low, close)):
        return False
    body = abs(close - open_)
    if direction == "long":
        lower_wick = min(open_, close) - low
        return lower_wick >= min_wick_frac * bar_range and body <= bar_range * 0.40
    if direction == "short":
        upper_wick = high - max(open_, close)
        return upper_wick >= min_wick_frac * bar_range and body <= bar_range * 0.40
    return False


def engulfing_confirm(
    direction: str,
    open_: float,
    high: float,
    low: float,
    close: float,
    prev_open: float,
    prev_high: float,
    prev_low: float,
    prev_close: float,
) -> bool:
    if min(high, low, close, open_, prev_high, prev_low, prev_close, prev_open) <= 0.0:
        return False
    if direction == "long":
        return (
            close > open_
            and prev_close < prev_open
            and close >= prev_open
            and open_ <= prev_close
        )
    if direction == "short":
        return (
            close < open_
            and prev_close > prev_open
            and close <= prev_open
            and open_ >= prev_close
        )
    return False


def overshoot_reclaim_valid(
    direction: str,
    level: float,
    atr: float,
    bar_high: float,
    bar_low: float,
    bar_close: float,
    *,
    through_mult: float = 0.3,
    back_threshold: float | None = None,
) -> bool:
    if level <= 0.0 or atr <= 0.0:
        return False
    back = back_threshold if back_threshold is not None else atr * 0.10
    if direction == "long":
        return bar_low < level - atr * through_mult and bar_close > level + back
    if direction == "short":
        return bar_high > level + atr * through_mult and bar_close < level - back
    return False
