"""Spec detector — see STRATEGY_CATALOG."""
from __future__ import annotations

import polars as pl

from ._common import SpecHit, as_float, with_spec_columns, _latest_values

__all__ = ["detect_ema_bounce"]

def detect_ema_bounce(frame: pl.DataFrame, *, timeframe: str = "15m") -> SpecHit | None:
    work = with_spec_columns(frame)
    if work.height < 45:
        return None
    row = _latest_values(work)
    atr = row.get("spec_atr14", 0.0)
    if atr <= 0.0 or row.get("spec_body_ratio", 0.0) <= 0.35:
        return None
    ema200 = row.get("spec_ema200", 0.0) or row.get("ema200", 0.0)
    for period in (9, 21, 50, 200):
        ema = row.get(f"spec_ema{period}", 0.0)
        if ema <= 0.0:
            continue
        if ema200 > 0.0 and row["close"] > ema200 and row["low"] <= ema and row["close"] > ema:
            return SpecHit(
                strategy="ema_bounce",
                direction="long",
                entry=row["close"],
                stop_basis=row["low"],
                atr=atr,
                timeframe=timeframe,
                reasons=(f"ema{period}_bounce", f"body_ratio={row['spec_body_ratio']:.2f}"),
                vol_ratio=row.get("volume_ratio20", 1.0),
                rsi=row.get("rsi14", 50.0),
            )
        if ema200 > 0.0 and row["close"] < ema200 and row["high"] >= ema and row["close"] < ema:
            return SpecHit(
                strategy="ema_bounce",
                direction="short",
                entry=row["close"],
                stop_basis=row["high"],
                atr=atr,
                timeframe=timeframe,
                reasons=(f"ema{period}_bounce", f"body_ratio={row['spec_body_ratio']:.2f}"),
                vol_ratio=row.get("volume_ratio20", 1.0),
                rsi=row.get("rsi14", 50.0),
            )
    return None



