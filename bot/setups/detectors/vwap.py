"""Spec detector — see STRATEGY_CATALOG."""
from __future__ import annotations

import polars as pl

from ._common import SpecHit, as_float, with_spec_columns

__all__ = ["detect_vwap_reclaim"]

def detect_vwap_reclaim(frame: pl.DataFrame, *, timeframe: str = "15m") -> SpecHit | None:
    work = with_spec_columns(frame)
    if work.height < 25:
        return None
    atr = as_float(work.item(-1, "spec_atr14"))
    if atr <= 0.0:
        return None
    prev_close = as_float(work.item(-2, "close"))
    prev_vwap = as_float(work.item(-2, "vwap"))
    close = as_float(work.item(-1, "close"))
    vwap = as_float(work.item(-1, "vwap"))
    if prev_close < prev_vwap and close > vwap:
        return SpecHit(
            strategy="vwap_trend",
            direction="long",
            entry=close,
            stop_basis=min(as_float(work.item(-1, "low")), vwap),
            atr=atr,
            timeframe=timeframe,
            reasons=(f"vwap_reclaim={vwap:.4f}",),
            vol_ratio=as_float(work.item(-1, "volume_ratio20"), 1.0),
            rsi=as_float(work.item(-1, "rsi14"), 50.0),
        )
    if prev_close > prev_vwap and close < vwap:
        return SpecHit(
            strategy="vwap_trend",
            direction="short",
            entry=close,
            stop_basis=max(as_float(work.item(-1, "high")), vwap),
            atr=atr,
            timeframe=timeframe,
            reasons=(f"vwap_reject={vwap:.4f}",),
            vol_ratio=as_float(work.item(-1, "volume_ratio20"), 1.0),
            rsi=as_float(work.item(-1, "rsi14"), 50.0),
        )
    return None



