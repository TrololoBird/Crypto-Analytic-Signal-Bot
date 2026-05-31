"""Spec detector — see STRATEGY_CATALOG."""
from __future__ import annotations

import polars as pl

from ._common import SpecHit, as_float, with_spec_columns, _latest_values, _valid_order_block_rows

__all__ = ["detect_breaker_block"]

def detect_breaker_block(frame: pl.DataFrame, *, timeframe: str = "1h") -> SpecHit | None:
    work = with_spec_columns(frame)
    if work.height < 35:
        return None
    current = _latest_values(work)
    atr = current.get("spec_atr14", 0.0)
    if atr <= 0.0:
        return None
    rows = work.to_dicts()
    current_low = current["low"]
    current_high = current["high"]
    current_close = current["close"]
    for zone in reversed(_valid_order_block_rows(work, max_age=120)):
        if not bool(zone.get("volume_ok")):
            continue
        direction = str(zone["direction"])
        bottom = as_float(zone.get("bottom"))
        top = as_float(zone.get("top"))
        source_idx = int(zone["_spec_idx"])
        break_rows = [row for row in rows if int(row["_spec_idx"]) > source_idx]
        if direction == "long":
            broken = any(as_float(row.get("close")) < bottom for row in break_rows)
            retested = current_high >= bottom and current_close < top
            if broken and retested:
                return SpecHit(
                    strategy="breaker_block",
                    direction="short",
                    entry=(bottom + top) / 2.0,
                    stop_basis=top,
                    atr=atr,
                    timeframe=timeframe,
                    reasons=(f"bull_ob_flipped_resistance={bottom:.4f}-{top:.4f}",),
                    structure_clarity=0.74,
                    vol_ratio=current.get("volume_ratio20", 1.0),
                    rsi=current.get("rsi14", 50.0),
                    source_index=source_idx,
                )
        else:
            broken = any(as_float(row.get("close")) > top for row in break_rows)
            retested = current_low <= top and current_close > bottom
            if broken and retested:
                return SpecHit(
                    strategy="breaker_block",
                    direction="long",
                    entry=(bottom + top) / 2.0,
                    stop_basis=bottom,
                    atr=atr,
                    timeframe=timeframe,
                    reasons=(f"bear_ob_flipped_support={bottom:.4f}-{top:.4f}",),
                    structure_clarity=0.74,
                    vol_ratio=current.get("volume_ratio20", 1.0),
                    rsi=current.get("rsi14", 50.0),
                    source_index=source_idx,
                )
    return None



