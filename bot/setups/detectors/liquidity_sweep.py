"""Spec detector — see STRATEGY_CATALOG."""
from __future__ import annotations

import polars as pl

from ._common import SpecHit, as_float, with_spec_columns, _latest_values

__all__ = ["detect_liquidity_sweep"]

def detect_liquidity_sweep(frame: pl.DataFrame, *, timeframe: str = "15m") -> SpecHit | None:
    work = with_spec_columns(frame)
    if work.height < 25:
        return None
    row = _latest_values(work)
    atr = row.get("spec_atr14", 0.0)
    prev_high = row.get("spec_prev_high20", 0.0)
    prev_low = row.get("spec_prev_low20", 0.0)
    close = row.get("close", 0.0)
    high = row.get("high", 0.0)
    low = row.get("low", 0.0)
    vol_ratio = row.get("volume_ratio20", 1.0)
    rsi = row.get("rsi14", 50.0)
    if atr <= 0.0:
        return None
    if high > prev_high and close < prev_high and (high - close) > atr * 0.3:
        return SpecHit(
            strategy="liquidity_sweep",
            direction="short",
            entry=prev_high,
            stop_basis=high,
            atr=atr,
            timeframe=timeframe,
            reasons=(f"sweep_high level={prev_high:.4f}", f"wick_atr={(high-close)/atr:.2f}"),
            vol_ratio=vol_ratio,
            rsi=rsi,
        )
    if low < prev_low and close > prev_low and (close - low) > atr * 0.3:
        return SpecHit(
            strategy="liquidity_sweep",
            direction="long",
            entry=prev_low,
            stop_basis=low,
            atr=atr,
            timeframe=timeframe,
            reasons=(f"sweep_low level={prev_low:.4f}", f"wick_atr={(close-low)/atr:.2f}"),
            vol_ratio=vol_ratio,
            rsi=rsi,
        )
    return None



