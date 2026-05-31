"""Spec detector — see STRATEGY_CATALOG."""
from __future__ import annotations

import polars as pl

from ._common import SpecHit, as_float, with_spec_columns, _latest_values

__all__ = ["detect_keltner_breakout"]

def detect_keltner_breakout(
    frame: pl.DataFrame,
    *,
    timeframe: str = "15m",
    lookback_bars: int = 3,
    min_volume_ratio: float = 1.30,
    kc_atr_mult: float = 1.80,
    acceptance_band_pct: float = 0.01,
    min_body_ratio: float = 0.45,
) -> SpecHit | None:
    work = with_spec_columns(frame)
    if work.height < 25:
        return None
    channel_mult = max(1.0, min(float(kc_atr_mult), 3.0))
    work = work.with_columns(
        [
            (pl.col("spec_ema20") + channel_mult * pl.col("spec_atr14")).alias(
                "spec_kc_break_upper"
            ),
            (pl.col("spec_ema20") - channel_mult * pl.col("spec_atr14")).alias(
                "spec_kc_break_lower"
            ),
        ]
    )
    current = _latest_values(work)
    current_close = current.get("close", 0.0)
    current_upper = current.get("spec_kc_break_upper", 0.0)
    current_lower = current.get("spec_kc_break_lower", 0.0)
    current_atr = current.get("spec_atr14", 0.0)
    if min(current_close, current_upper, current_atr) <= 0.0 or current_lower <= 0.0:
        return None
    recent_count = max(1, min(int(lookback_bars), 5, work.height))
    recent = work.tail(recent_count).to_dicts()
    current_idx = int(current.get("_spec_idx", work.height - 1))
    min_vol = max(0.0, float(min_volume_ratio))
    body_floor = max(0.0, min(float(min_body_ratio), 1.0))
    hold_band = max(0.0, min(float(acceptance_band_pct), 0.05))
    current_holds_long = current_close >= current_upper * (1.0 - hold_band)
    current_holds_short = current_close <= current_lower * (1.0 + hold_band)

    for row in reversed(recent):
        idx = int(row.get("_spec_idx", current_idx))
        lag = current_idx - idx
        if lag < 0 or lag >= recent_count:
            continue
        atr = as_float(row.get("spec_atr14"))
        upper = as_float(row.get("spec_kc_break_upper"))
        lower = as_float(row.get("spec_kc_break_lower"))
        volume_mean = as_float(row.get("spec_volume_mean20"))
        volume_ratio = as_float(row.get("volume_ratio20"), 0.0)
        if volume_ratio <= 0.0 and volume_mean > 0.0:
            volume_ratio = as_float(row.get("volume")) / volume_mean
        if atr <= 0.0 or upper <= 0.0 or lower <= 0.0 or volume_ratio < min_vol:
            continue
        body_ratio = as_float(row.get("spec_body_ratio"))
        open_price = as_float(row.get("open"))
        close_price = as_float(row.get("close"))
        directional_body = body_ratio >= body_floor
        if (
            current_holds_long
            and close_price > upper
            and close_price > open_price
            and directional_body
        ):
            return SpecHit(
                strategy="keltner_breakout",
                direction="long",
                entry=max(current_upper, upper),
                stop_basis=as_float(row.get("low")),
                atr=atr,
                timeframe=timeframe,
                reasons=(
                    f"kc_upper_recent_break={upper:.4f}",
                    f"breakout_lag={lag}",
                    f"kc_mult={channel_mult:.2f}",
                    f"acceptance={current_close / current_upper:.4f}",
                ),
                vol_ratio=volume_ratio,
                rsi=current.get("rsi14", 50.0),
                source_index=idx,
            )
        if (
            current_holds_short
            and close_price < lower
            and close_price < open_price
            and directional_body
        ):
            return SpecHit(
                strategy="keltner_breakout",
                direction="short",
                entry=min(current_lower, lower),
                stop_basis=as_float(row.get("high")),
                atr=atr,
                timeframe=timeframe,
                reasons=(
                    f"kc_lower_recent_break={lower:.4f}",
                    f"breakout_lag={lag}",
                    f"kc_mult={channel_mult:.2f}",
                    f"acceptance={current_close / current_lower:.4f}",
                ),
                vol_ratio=volume_ratio,
                rsi=current.get("rsi14", 50.0),
                source_index=idx,
            )
    return None



