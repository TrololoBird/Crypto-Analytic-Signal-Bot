"""Spec + prepared detector."""
from __future__ import annotations

import polars as pl

from ._common import SpecHit, as_float, finite_or_none, with_spec_columns, _latest_values, build_spec_signal
from ...domain.config import BotSettings
from ...domain.schemas import PreparedSymbol, Signal
from ...domain.strategy_catalog import catalog_default_params
from ._roadmap import _build_atr_signal, _last, _reject, _as_float

__all__ = ['detect_stop_hunt', 'detect_stop_hunt_prepared']


def detect_stop_hunt(frame: pl.DataFrame, *, timeframe: str = "15m") -> SpecHit | None:
    work = with_spec_columns(frame)
    if work.height < 20:
        return None
    row = _latest_values(work)
    atr = row.get("spec_atr14", 0.0)
    if atr <= 0.0:
        return None
    recent = work.tail(10)
    high = row["high"]
    low = row["low"]
    close = row["close"]
    prev_high = row.get("spec_prev_high20", 0.0)
    prev_low = row.get("spec_prev_low20", 0.0)
    upper_wick = row.get("spec_upper_wick", 0.0)
    lower_wick = row.get("spec_lower_wick", 0.0)
    upper_ratio = row.get("spec_upper_wick_ratio", 0.0)
    lower_ratio = row.get("spec_lower_wick_ratio", 0.0)
    high_touches = recent.filter((pl.col("high") - prev_high).abs() <= atr * 0.35).height
    low_touches = recent.filter((pl.col("low") - prev_low).abs() <= atr * 0.35).height
    if (
        high > prev_high
        and close < prev_high
        and upper_ratio > 1.35
        and upper_wick > atr * 0.35
        and high_touches >= 1
    ):
        return SpecHit(
            strategy="stop_hunt_detection",
            direction="short",
            entry=prev_high,
            stop_basis=high,
            atr=atr,
            timeframe=timeframe,
            reasons=(f"stop_cluster_high={prev_high:.4f}", f"touches={high_touches}"),
            vol_ratio=row.get("volume_ratio20", 1.0),
            rsi=row.get("rsi14", 50.0),
        )
    if (
        low < prev_low
        and close > prev_low
        and lower_ratio > 1.35
        and lower_wick > atr * 0.35
        and low_touches >= 1
    ):
        return SpecHit(
            strategy="stop_hunt_detection",
            direction="long",
            entry=prev_low,
            stop_basis=low,
            atr=atr,
            timeframe=timeframe,
            reasons=(f"stop_cluster_low={prev_low:.4f}", f"touches={low_touches}"),
            vol_ratio=row.get("volume_ratio20", 1.0),
            rsi=row.get("rsi14", 50.0),
        )
    return None




