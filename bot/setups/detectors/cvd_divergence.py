"""Spec detector — see STRATEGY_CATALOG."""
from __future__ import annotations

import polars as pl

from ._common import SpecHit, as_float, with_spec_columns, _pivot_rows

__all__ = ["detect_cvd_divergence"]

def detect_cvd_divergence(frame: pl.DataFrame, *, timeframe: str = "15m") -> SpecHit | None:
    work = with_spec_columns(frame)
    if work.height < 60:
        return None
    atr = as_float(work.item(-1, "spec_atr14"))
    delta_std = as_float(work.item(-1, "spec_delta_std20"))
    if atr <= 0.0 or delta_std <= 0.0:
        return None
    lows = _pivot_rows(work, price_column="low", indicator_column="spec_cvd", pivot="low")
    if len(lows) >= 2:
        old, new = lows[-2], lows[-1]
        cvd_shift = new["indicator"] - old["indicator"]
        if new["price"] < old["price"] and cvd_shift > 1.5 * delta_std:
            return SpecHit(
                strategy="cvd_divergence",
                direction="long",
                entry=as_float(work.item(-1, "close")),
                stop_basis=new["price"],
                atr=atr,
                timeframe=timeframe,
                reasons=(f"price_ll_cvd_hl shift={cvd_shift:.4f}",),
                rsi=as_float(work.item(-1, "rsi14"), 50.0),
            )
    highs = _pivot_rows(work, price_column="high", indicator_column="spec_cvd", pivot="high")
    if len(highs) >= 2:
        old, new = highs[-2], highs[-1]
        cvd_shift = old["indicator"] - new["indicator"]
        if new["price"] > old["price"] and cvd_shift > 1.5 * delta_std:
            return SpecHit(
                strategy="cvd_divergence",
                direction="short",
                entry=as_float(work.item(-1, "close")),
                stop_basis=new["price"],
                atr=atr,
                timeframe=timeframe,
                reasons=(f"price_hh_cvd_lh shift={cvd_shift:.4f}",),
                rsi=as_float(work.item(-1, "rsi14"), 50.0),
            )
    return None



