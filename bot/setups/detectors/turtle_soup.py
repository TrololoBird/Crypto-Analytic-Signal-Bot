"""Spec detector — see STRATEGY_CATALOG."""
from __future__ import annotations

import polars as pl

from ._common import SpecHit, as_float, with_spec_columns, _latest_values

__all__ = ["detect_turtle_soup"]

def detect_turtle_soup(frame: pl.DataFrame, *, timeframe: str = "15m") -> SpecHit | None:
    work = with_spec_columns(frame)
    if work.height < 25:
        return None
    row = _latest_values(work)
    atr = row.get("spec_atr14", 0.0)
    if atr <= 0.0:
        return None
    high = row["high"]
    low = row["low"]
    close = row["close"]
    upper = row.get("spec_prev_high20", 0.0)
    lower = row.get("spec_prev_low20", 0.0)
    vol_ratio = row.get("volume_ratio20", 1.0)
    rsi = row.get("rsi14", 50.0)
    if low < lower and close > lower:
        return SpecHit(
            strategy="turtle_soup",
            direction="long",
            entry=lower,
            stop_basis=low,
            atr=atr,
            timeframe=timeframe,
            reasons=(f"donchian_false_break_low={lower:.4f}",),
            vol_ratio=vol_ratio,
            rsi=rsi,
        )
    if high > upper and close < upper:
        return SpecHit(
            strategy="turtle_soup",
            direction="short",
            entry=upper,
            stop_basis=high,
            atr=atr,
            timeframe=timeframe,
            reasons=(f"donchian_false_break_high={upper:.4f}",),
            vol_ratio=vol_ratio,
            rsi=rsi,
        )
    return None



