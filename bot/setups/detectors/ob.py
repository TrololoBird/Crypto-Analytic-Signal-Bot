"""Spec detector — see STRATEGY_CATALOG."""
from __future__ import annotations

import polars as pl

from ._common import SpecHit, as_float, with_spec_columns, _latest_values, _valid_order_block_rows

__all__ = ["detect_order_block"]


def detect_order_block(frame: pl.DataFrame, *, timeframe: str = "1h") -> SpecHit | None:
    work = with_spec_columns(frame)
    if work.height < 30:
        return None
    current = _latest_values(work)
    atr = current.get("spec_atr14", 0.0)
    close = current.get("close", 0.0)
    if atr <= 0.0 or close <= 0.0:
        return None
    for zone in reversed(_valid_order_block_rows(work)):
        bottom = as_float(zone.get("bottom"))
        top = as_float(zone.get("top"))
        if bottom <= close <= top:
            direction = str(zone["direction"])
            return SpecHit(
                strategy="order_block",
                direction=direction,
                entry=(bottom + top) / 2.0,
                stop_basis=bottom if direction == "long" else top,
                atr=atr,
                timeframe=timeframe,
                reasons=(f"ob_zone={bottom:.4f}-{top:.4f}", f"age={int(zone['age'])}"),
                structure_clarity=0.76,
                vol_ratio=current.get("volume_ratio20", 1.0),
                rsi=current.get("rsi14", 50.0),
                source_index=int(zone["_spec_idx"]),
            )
    return None



