"""Spec detector — see STRATEGY_CATALOG."""
from __future__ import annotations

import polars as pl

from ._common import SpecHit, as_float, with_spec_columns, _latest_values

__all__ = ["detect_volume_anomaly"]

def detect_volume_anomaly(frame: pl.DataFrame, *, timeframe: str = "15m") -> SpecHit | None:
    work = with_spec_columns(frame)
    if work.height < 25:
        return None
    row = _latest_values(work)
    atr = row.get("spec_atr14", 0.0)
    volume_mean = row.get("spec_volume_mean20", 0.0)
    if atr <= 0.0 or volume_mean <= 0.0:
        return None
    if row["volume"] <= volume_mean * 3.0 or row.get("spec_body_ratio", 0.0) <= 0.6:
        return None
    direction = "long" if row["close"] > row["open"] else "short"
    stop_basis = row["low"] if direction == "long" else row["high"]
    return SpecHit(
        strategy="volume_anomaly",
        direction=direction,
        entry=row["close"],
        stop_basis=stop_basis,
        atr=atr,
        timeframe=timeframe,
        reasons=(f"volume_spike={row['volume']/volume_mean:.2f}x", f"body_ratio={row['spec_body_ratio']:.2f}"),
        structure_clarity=row.get("spec_body_ratio", 0.6),
        vol_ratio=row.get("volume_ratio20", row["volume"] / volume_mean),
        rsi=row.get("rsi14", 50.0),
    )



