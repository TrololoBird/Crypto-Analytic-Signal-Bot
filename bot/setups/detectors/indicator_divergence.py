"""Spec detector — see STRATEGY_CATALOG."""
from __future__ import annotations

import polars as pl

from ._common import SpecHit, as_float, with_spec_columns, _pivot_rows

__all__ = ["detect_regular_divergence"]

def detect_regular_divergence(
    frame: pl.DataFrame,
    *,
    timeframe: str = "15m",
    require_oversold: bool = False,
) -> SpecHit | None:
    work = with_spec_columns(frame)
    if work.height < 48:
        return None
    atr = as_float(work.item(-1, "spec_atr14"))
    if atr <= 0.0:
        return None
    lows = _pivot_rows(work, price_column="low", indicator_column="rsi14", pivot="low")
    if len(lows) >= 2:
        old, new = lows[-2], lows[-1]
        if new["price"] < old["price"] and new["indicator"] > old["indicator"]:
            if not require_oversold or min(old["indicator"], new["indicator"]) < 35.0:
                strategy = "rsi_divergence_bottom" if require_oversold else "indicator_divergence"
                return SpecHit(
                    strategy=strategy,
                    direction="long",
                    entry=as_float(work.item(-1, "close")),
                    stop_basis=new["price"],
                    atr=atr,
                    timeframe=timeframe,
                    reasons=(
                        f"regular_bullish_div price_ll={new['price']:.4f}",
                        f"rsi_hl={new['indicator']:.1f}",
                    ),
                    rsi=as_float(work.item(-1, "rsi14"), 50.0),
                )
    highs = _pivot_rows(work, price_column="high", indicator_column="rsi14", pivot="high")
    if len(highs) >= 2 and not require_oversold:
        old, new = highs[-2], highs[-1]
        if new["price"] > old["price"] and new["indicator"] < old["indicator"]:
            return SpecHit(
                strategy="indicator_divergence",
                direction="short",
                entry=as_float(work.item(-1, "close")),
                stop_basis=new["price"],
                atr=atr,
                timeframe=timeframe,
                reasons=(
                    f"regular_bearish_div price_hh={new['price']:.4f}",
                    f"rsi_lh={new['indicator']:.1f}",
                ),
                rsi=as_float(work.item(-1, "rsi14"), 50.0),
            )
    return None



