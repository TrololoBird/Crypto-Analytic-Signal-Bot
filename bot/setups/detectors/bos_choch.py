"""Spec detector — see STRATEGY_CATALOG."""
from __future__ import annotations

import polars as pl

from ._common import SpecHit, as_float, with_spec_columns, _latest_values, _row_volume_ratio

__all__ = ["detect_bos_choch"]

def detect_bos_choch(
    frame: pl.DataFrame,
    *,
    timeframe: str = "15m",
    max_age: int = 28,
) -> SpecHit | None:
    work = with_spec_columns(frame)
    if work.height < 25:
        return None
    current = _latest_values(work)
    current_idx = int(work.item(-1, "_spec_idx"))
    current_close = current.get("close", 0.0)
    current_atr = current.get("spec_atr14", 0.0)
    if current_close <= 0.0 or current_atr <= 0.0:
        return None
    candidates = work.tail(max_age + 1).to_dicts()
    for row in reversed(candidates):
        idx = int(row["_spec_idx"])
        age = current_idx - idx
        if age > max_age:
            continue
        close = as_float(row.get("close"))
        prev_high = as_float(row.get("spec_prev_high20"))
        prev_low = as_float(row.get("spec_prev_low20"))
        atr = as_float(row.get("spec_atr14"), current_atr)
        if min(close, prev_high, prev_low, atr) <= 0.0:
            continue
        if close > prev_high and current_close > prev_high:
            return SpecHit(
                strategy="bos_choch",
                direction="long",
                entry=current_close if age == 0 else prev_high,
                stop_basis=prev_high - atr,
                atr=atr,
                timeframe=timeframe,
                reasons=(f"body_break_above_swing={prev_high:.4f}", f"break_age={age}"),
                structure_clarity=min(1.0, (close - prev_high) / max(atr, 1e-8)),
                vol_ratio=_row_volume_ratio(row),
                rsi=as_float(row.get("rsi14"), 50.0),
                source_index=idx,
            )
        if close < prev_low and current_close < prev_low:
            return SpecHit(
                strategy="bos_choch",
                direction="short",
                entry=current_close if age == 0 else prev_low,
                stop_basis=prev_low + atr,
                atr=atr,
                timeframe=timeframe,
                reasons=(f"body_break_below_swing={prev_low:.4f}", f"break_age={age}"),
                structure_clarity=min(1.0, (prev_low - close) / max(atr, 1e-8)),
                vol_ratio=_row_volume_ratio(row),
                rsi=as_float(row.get("rsi14"), 50.0),
                source_index=idx,
            )
    return None



