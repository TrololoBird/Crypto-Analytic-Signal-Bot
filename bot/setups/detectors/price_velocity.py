"""Spec detector — see STRATEGY_CATALOG."""
from __future__ import annotations

import polars as pl

from ._common import SpecHit, as_float, with_spec_columns

__all__ = ["detect_price_velocity"]

def detect_price_velocity(
    frame: pl.DataFrame,
    *,
    timeframe: str = "15m",
    lookback: int = 5,
    threshold: float = 0.5,
) -> SpecHit | None:
    work = with_spec_columns(frame)
    if work.height < lookback + 20:
        return None
    close = as_float(work.item(-1, "close"))
    prior = as_float(work.item(-1 - lookback, "close"))
    atr = as_float(work.item(-1, "spec_atr14"))
    if min(close, prior, atr) <= 0.0:
        return None
    velocity_norm = ((close - prior) / lookback) / atr
    if abs(velocity_norm) <= threshold:
        return None
    direction = "long" if velocity_norm > 0.0 else "short"
    return SpecHit(
        strategy="price_velocity",
        direction=direction,
        entry=close,
        stop_basis=as_float(work.item(-1, "low" if direction == "long" else "high")),
        atr=atr,
        timeframe=timeframe,
        reasons=(f"velocity_norm={velocity_norm:.2f}",),
        structure_clarity=min(1.0, abs(velocity_norm)),
        vol_ratio=as_float(work.item(-1, "volume_ratio20"), 1.0),
        rsi=as_float(work.item(-1, "rsi14"), 50.0),
    )



