"""Spec detector — see STRATEGY_CATALOG."""
from __future__ import annotations

import polars as pl

from ._common import SpecHit, as_float, with_spec_columns

__all__ = ["detect_volume_climax_reversal"]

def detect_volume_climax_reversal(frame: pl.DataFrame, *, timeframe: str = "15m") -> SpecHit | None:
    work = with_spec_columns(frame)
    if work.height < 30:
        return None
    current_close = as_float(work.item(-1, "close"))
    current_idx = int(work.item(-1, "_spec_idx"))
    recent = work.tail(4).to_dicts()
    for row in reversed(recent[:-1]):
        idx = int(row["_spec_idx"])
        lag = current_idx - idx
        if lag < 1 or lag > 3:
            continue
        atr = as_float(row.get("spec_atr14"))
        volume_mean = as_float(row.get("spec_volume_mean20"))
        if atr <= 0.0 or volume_mean <= 0.0 or as_float(row.get("volume")) <= volume_mean * 5.0:
            continue
        midpoint = (as_float(row.get("high")) + as_float(row.get("low"))) / 2.0
        prev_high = as_float(row.get("spec_prev_high20"))
        prev_low = as_float(row.get("spec_prev_low20"))
        if as_float(row.get("low")) < prev_low and current_close > midpoint:
            return SpecHit(
                strategy="volume_climax_reversal",
                direction="long",
                entry=current_close,
                stop_basis=as_float(row.get("low")),
                atr=atr,
                timeframe=timeframe,
                reasons=(f"sell_climax_reclaimed_mid={midpoint:.4f}", f"lag={lag}"),
                vol_ratio=as_float(row.get("volume_ratio20"), 1.0),
                rsi=as_float(work.item(-1, "rsi14"), 50.0),
            )
        if as_float(row.get("high")) > prev_high and current_close < midpoint:
            return SpecHit(
                strategy="volume_climax_reversal",
                direction="short",
                entry=current_close,
                stop_basis=as_float(row.get("high")),
                atr=atr,
                timeframe=timeframe,
                reasons=(f"buy_climax_reclaimed_mid={midpoint:.4f}", f"lag={lag}"),
                vol_ratio=as_float(row.get("volume_ratio20"), 1.0),
                rsi=as_float(work.item(-1, "rsi14"), 50.0),
            )
    return None



