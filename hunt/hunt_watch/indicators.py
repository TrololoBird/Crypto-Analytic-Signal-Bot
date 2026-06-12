"""Minimal indicators for hunt lite path (young listings, short history)."""

from __future__ import annotations

import math
from typing import Any

import polars as pl

from hunt_core.features.prepare_frame import _rsi

RSI_MIN_BARS = 15  # Wilder RSI-14 seed (seed_offset=1 + period)


def _as_polars(df: Any) -> pl.DataFrame | None:
    if df is None:
        return None
    if isinstance(df, pl.DataFrame):
        return df if not df.is_empty() else None
    return None


def rsi14_from_ohlc(df: Any, *, idx: int = -1) -> float | None:
    """Wilder RSI-14 on raw OHLC — matches bot.features.prepare_frame._rsi semantics."""
    frame = _as_polars(df)
    if frame is None or "close" not in frame.columns or frame.height < RSI_MIN_BARS:
        return None
    try:
        series = _rsi(frame, period=14)
        pos = idx if idx >= 0 else frame.height + idx
        if pos < 0 or pos >= series.len():
            return None
        value = float(series[pos])
    except (TypeError, ValueError, IndexError):
        return None
    if not math.isfinite(value):
        return None
    return value
