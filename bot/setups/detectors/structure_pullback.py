"""Spec detector — see STRATEGY_CATALOG."""
from __future__ import annotations

import polars as pl

from ._common import SpecHit, as_float, with_spec_columns, _latest_values, _clean_impulse

__all__ = ["detect_structure_pullback"]

def detect_structure_pullback(frame: pl.DataFrame, *, timeframe: str = "15m") -> SpecHit | None:
    work = with_spec_columns(frame)
    if work.height < 35:
        return None
    current = _latest_values(work)
    atr = current.get("spec_atr14", 0.0)
    if atr <= 0.0:
        return None
    window = work.tail(30)
    rows = window.to_dicts()
    lows = [(i, as_float(row.get("low"))) for i, row in enumerate(rows)]
    highs = [(i, as_float(row.get("high"))) for i, row in enumerate(rows)]
    low_pos, swing_low = min(lows, key=lambda item: item[1])
    high_pos, swing_high = max(highs, key=lambda item: item[1])
    close = current["close"]
    if swing_high <= swing_low:
        return None
    if close > current.get("spec_ema50", close) and low_pos < high_pos:
        if not _clean_impulse(window, low_pos, high_pos, "long"):
            return None
        fib50 = swing_low + 0.5 * (swing_high - swing_low)
        fib618 = swing_low + 0.618 * (swing_high - swing_low)
        vol_ratio = current.get("volume_ratio20", 1.0)
        if vol_ratio < 0.85:
            return None
        if fib50 <= close <= fib618:
            return SpecHit(
                strategy="structure_pullback",
                direction="long",
                entry=close,
                stop_basis=swing_low,
                atr=atr,
                timeframe=timeframe,
                reasons=(f"ote_zone={fib50:.4f}-{fib618:.4f}", "clean_bull_impulse"),
                structure_clarity=0.70,
                vol_ratio=current.get("volume_ratio20", 1.0),
                rsi=current.get("rsi14", 50.0),
            )
    if close < current.get("spec_ema50", close) and high_pos < low_pos:
        if not _clean_impulse(window, high_pos, low_pos, "short"):
            return None
        fib50 = swing_high - 0.5 * (swing_high - swing_low)
        fib618 = swing_high - 0.618 * (swing_high - swing_low)
        zone_low = min(fib50, fib618)
        zone_high = max(fib50, fib618)
        vol_ratio = current.get("volume_ratio20", 1.0)
        if vol_ratio < 0.85:
            return None
        if zone_low <= close <= zone_high:
            return SpecHit(
                strategy="structure_pullback",
                direction="short",
                entry=close,
                stop_basis=swing_high,
                atr=atr,
                timeframe=timeframe,
                reasons=(f"ote_zone={zone_low:.4f}-{zone_high:.4f}", "clean_bear_impulse"),
                structure_clarity=0.70,
                vol_ratio=current.get("volume_ratio20", 1.0),
                rsi=current.get("rsi14", 50.0),
            )
    return None



