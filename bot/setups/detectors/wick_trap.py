"""Spec detector — see STRATEGY_CATALOG."""
from __future__ import annotations

import polars as pl

from ._common import SpecHit, as_float, with_spec_columns, _latest_values

__all__ = ["detect_wick_trap"]

def detect_wick_trap(frame: pl.DataFrame, *, timeframe: str = "15m") -> SpecHit | None:
    work = with_spec_columns(frame)
    if work.height < 25:
        return None
    row = _latest_values(work)
    atr = row.get("spec_atr14", 0.0)
    body = row.get("spec_body", 0.0)
    if atr <= 0.0 or body < atr * 0.25:
        return None
    prev_high = row.get("spec_prev_high20", 0.0)
    prev_low = row.get("spec_prev_low20", 0.0)
    vol_ratio = row.get("volume_ratio20", 1.0)
    wick_mult = 1.1
    lower_wick = row.get("spec_lower_wick_ratio", 0.0)
    upper_wick = row.get("spec_upper_wick_ratio", 0.0)
    if row["low"] < prev_low and lower_wick >= wick_mult and vol_ratio >= 0.85:
        return SpecHit(
            strategy="wick_trap_reversal",
            direction="long",
            entry=row["close"],
            stop_basis=row["low"],
            atr=atr,
            timeframe=timeframe,
            reasons=(f"new_low_wick_trap={prev_low:.4f}", f"body_atr={body/atr:.2f}"),
            vol_ratio=row.get("volume_ratio20", 1.0),
            rsi=row.get("rsi14", 50.0),
        )
    if row["high"] > prev_high and upper_wick >= wick_mult and vol_ratio >= 0.85:
        return SpecHit(
            strategy="wick_trap_reversal",
            direction="short",
            entry=row["close"],
            stop_basis=row["high"],
            atr=atr,
            timeframe=timeframe,
            reasons=(f"new_high_wick_trap={prev_high:.4f}", f"body_atr={body/atr:.2f}"),
            vol_ratio=row.get("volume_ratio20", 1.0),
            rsi=row.get("rsi14", 50.0),
        )
    return None



