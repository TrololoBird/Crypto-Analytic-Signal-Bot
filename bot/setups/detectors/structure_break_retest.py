"""Spec detector — see STRATEGY_CATALOG."""
from __future__ import annotations

import polars as pl

from ._common import SpecHit, as_float, with_spec_columns, _latest_values, _clean_impulse

__all__ = ["detect_structure_break_retest"]

def detect_structure_break_retest(
    frame: pl.DataFrame,
    *,
    timeframe: str = "15m",
    lookback: int = 20,
    tolerance_pct: float = 0.001,
) -> SpecHit | None:
    work = with_spec_columns(frame)
    if work.height < 25:
        return None
    current = _latest_values(work)
    atr = current.get("spec_atr14", 0.0)
    if atr <= 0.0:
        return None
    close = current["close"]
    low = current["low"]
    high = current["high"]
    current_idx = int(work.item(-1, "_spec_idx"))
    candidates = work.tail(lookback + 2).head(lookback + 1).to_dicts()
    for row in reversed(candidates):
        idx = int(row["_spec_idx"])
        age = current_idx - idx
        if age < 1 or age > lookback:
            continue
        break_close = as_float(row.get("close"))
        prev_high = as_float(row.get("spec_prev_high20"))
        prev_low = as_float(row.get("spec_prev_low20"))
        if min(break_close, prev_high, prev_low) <= 0.0:
            continue
        if (
            break_close > prev_high
            and low <= prev_high * (1.0 + tolerance_pct)
            and close > prev_high
        ):
            return SpecHit(
                strategy="structure_break_retest",
                direction="long",
                entry=prev_high,
                stop_basis=min(low, prev_high - atr * 0.25),
                atr=atr,
                timeframe=timeframe,
                reasons=(f"bos_retest_level={prev_high:.4f}", f"break_age={age}"),
                structure_clarity=0.72,
                vol_ratio=current.get("volume_ratio20", 1.0),
                rsi=current.get("rsi14", 50.0),
                source_index=idx,
            )
        if (
            break_close < prev_low
            and high >= prev_low * (1.0 - tolerance_pct)
            and close < prev_low
        ):
            return SpecHit(
                strategy="structure_break_retest",
                direction="short",
                entry=prev_low,
                stop_basis=max(high, prev_low + atr * 0.25),
                atr=atr,
                timeframe=timeframe,
                reasons=(f"bos_retest_level={prev_low:.4f}", f"break_age={age}"),
                structure_clarity=0.72,
                vol_ratio=current.get("volume_ratio20", 1.0),
                rsi=current.get("rsi14", 50.0),
                source_index=idx,
            )
    return None

