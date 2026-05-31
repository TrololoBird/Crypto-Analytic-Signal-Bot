"""Spec detector — see STRATEGY_CATALOG."""
from __future__ import annotations

import polars as pl

from ._common import SpecHit, as_float, with_spec_columns, _pivot_rows, _latest_values

__all__ = ["detect_hidden_divergence"]

def detect_hidden_divergence(frame: pl.DataFrame, *, timeframe: str = "15m") -> SpecHit | None:
    work = with_spec_columns(frame)
    if work.height < 60:
        return None
    row = _latest_values(work)
    atr = row.get("spec_atr14", 0.0)
    if atr <= 0.0:
        return None
    min_rsi_separation = 4.0
    lows = _pivot_rows(work, price_column="low", indicator_column="rsi14", pivot="low")
    if row["close"] > row.get("spec_ema50", row["close"]) and len(lows) >= 2:
        old, new = lows[-2], lows[-1]
        rsi_gap = old["indicator"] - new["indicator"]
        if (
            new["price"] > old["price"]
            and rsi_gap >= min_rsi_separation
        ):
            return SpecHit(
                strategy="hidden_divergence",
                direction="long",
                entry=row["close"],
                stop_basis=new["price"],
                atr=atr,
                timeframe=timeframe,
                reasons=(
                    f"hidden_bullish_div price_hl={new['price']:.4f}",
                    f"rsi_ll_gap={rsi_gap:.2f}",
                ),
                rsi=row.get("rsi14", 50.0),
            )
    highs = _pivot_rows(work, price_column="high", indicator_column="rsi14", pivot="high")
    if row["close"] < row.get("spec_ema50", row["close"]) and len(highs) >= 2:
        old, new = highs[-2], highs[-1]
        rsi_gap = new["indicator"] - old["indicator"]
        if (
            new["price"] < old["price"]
            and rsi_gap >= min_rsi_separation
        ):
            return SpecHit(
                strategy="hidden_divergence",
                direction="short",
                entry=row["close"],
                stop_basis=new["price"],
                atr=atr,
                timeframe=timeframe,
                reasons=(
                    f"hidden_bearish_div price_lh={new['price']:.4f}",
                    f"rsi_hh_gap={rsi_gap:.2f}",
                ),
                rsi=row.get("rsi14", 50.0),
            )
    return None



