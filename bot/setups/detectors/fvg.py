"""Spec detector — see STRATEGY_CATALOG."""
from __future__ import annotations

import polars as pl

from ._common import SpecHit, as_float, with_spec_columns

__all__ = ["detect_fvg"]

def detect_fvg(frame: pl.DataFrame, *, timeframe: str = "15m", max_age: int = 20) -> SpecHit | None:
    work = with_spec_columns(frame)
    if work.height < 5:
        return None
    work = work.with_columns(
        [
            pl.col("high").shift(2).alias("spec_h2"),
            pl.col("low").shift(2).alias("spec_l2"),
            (pl.col("low") > pl.col("high").shift(2)).alias("spec_bull_fvg"),
            (pl.col("high") < pl.col("low").shift(2)).alias("spec_bear_fvg"),
        ]
    )
    current_close = as_float(work.item(-1, "close"))
    current_idx = int(work.item(-1, "_spec_idx"))
    candidates = work.filter(pl.col("spec_bull_fvg") | pl.col("spec_bear_fvg")).tail(max_age + 1)
    for row in reversed(candidates.to_dicts()):
        idx = int(row["_spec_idx"])
        age = current_idx - idx
        if age > max_age:
            continue
        atr = as_float(row.get("spec_atr14"), as_float(work.item(-1, "spec_atr14")))
        vol_ratio = as_float(row.get("volume_ratio20"), 1.0)
        rsi = as_float(work.item(-1, "rsi14"), 50.0)
        if bool(row.get("spec_bull_fvg")):
            bottom = as_float(row.get("spec_h2"))
            top = as_float(row.get("low"))
            if bottom <= current_close <= top:
                return SpecHit(
                    strategy="fvg_setup",
                    direction="long",
                    entry=(bottom + top) / 2.0,
                    stop_basis=bottom,
                    atr=atr,
                    timeframe=timeframe,
                    reasons=(f"bull_fvg zone={bottom:.4f}-{top:.4f}", f"age={age}"),
                    vol_ratio=vol_ratio,
                    rsi=rsi,
                    source_index=idx,
                )
        if bool(row.get("spec_bear_fvg")):
            bottom = as_float(row.get("high"))
            top = as_float(row.get("spec_l2"))
            if bottom <= current_close <= top:
                return SpecHit(
                    strategy="fvg_setup",
                    direction="short",
                    entry=(bottom + top) / 2.0,
                    stop_basis=top,
                    atr=atr,
                    timeframe=timeframe,
                    reasons=(f"bear_fvg zone={bottom:.4f}-{top:.4f}", f"age={age}"),
                    vol_ratio=vol_ratio,
                    rsi=rsi,
                    source_index=idx,
                )
    return None

