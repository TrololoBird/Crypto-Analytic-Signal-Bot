"""Directional trend/VWAP filters for scoring and delivery report."""
from __future__ import annotations

from typing import Any

from hunt_core.analysis.adx_thresholds import ADX_TREND_MIN
from hunt_core.gate._rr import (
    DUMP_CONTINUATION_PHASES,
    FADE_PHASES_SHORT,
    PUMP_PHASES_LONG,
)
from hunt_core.params.store import effective_hunt_params, filter_thresholds

DI_DOMINANCE = 1.25

_PUMP_PREP_PHASES = frozenset(
    {
        "post_dump_bounce",
        "accumulation",
        "recovery",
        "breakout_arming",
        "impulse_initiating",
    }
)
_FADE_PREP_PHASES = frozenset({"exhaustion_at_high", "distribution"})


def _vwap_extreme_atr(symbol: str = "") -> float:
    flt = filter_thresholds(symbol)
    return float(flt.get("vwap_extreme_atr", 2.25))


def directional_filters(
    tf: dict[str, Any],
    *,
    direction: str,
    pos_in_range: float,
    symbol: str = "",
    lifecycle_phase: str = "",
    fall_from_high_pct: float = 0.0,
    chg_24h_pct: float | None = None,
) -> tuple[float, list[str], list[str]]:
    """Returns (score_delta, soft_triggers, hard_blocks)."""
    r1h = tf.get("1h") or {}
    r15 = tf.get("15m_closed") or tf.get("15m") or {}
    adx = float(r1h.get("adx14") or 0.0)
    plus = float(r1h.get("plus_di") or 0.0)
    minus = float(r1h.get("minus_di") or 0.0)
    st_dir = r1h.get("supertrend_dir")
    vdev = r15.get("vwap_dev_atr")
    obv_rising = r1h.get("obv_rising")

    delta = 0.0
    triggers: list[str] = []
    blocks: list[str] = []
    adx_block = effective_hunt_params(symbol).adx_trend_block
    vwap_extreme = _vwap_extreme_atr(symbol)
    phase = str(lifecycle_phase or "")
    mid_dump = phase == "dump_active" and fall_from_high_pct >= 12.0
    short_prep = phase in _FADE_PREP_PHASES
    pump_prep = phase in _PUMP_PREP_PHASES

    if direction == "short":
        if adx >= ADX_TREND_MIN and plus > 0 and plus > minus * DI_DOMINANCE:
            if mid_dump:
                delta -= 8.0
                triggers.append(f"adx_uptrend_mid_dump_soft_{adx:.0f}")
            elif short_prep:
                delta -= 8.0
                triggers.append(f"adx_uptrend_fade_prep_soft_{adx:.0f}")
            elif adx >= adx_block:
                blocks.append(f"adx1h_uptrend_{adx:.0f}")
            else:
                delta -= 15.0
                triggers.append("adx1h_uptrend_against_short")
        if st_dir == 1:
            delta -= 8.0
            triggers.append("headwind_supertrend_1h_up")
        if vdev is not None and float(vdev) <= -vwap_extreme:
            vdev_f = float(vdev)
            dump_leg = (
                mid_dump
                or phase in {"dump_active", "distribution"}
                or (phase == "impulse_initiating" and fall_from_high_pct >= 8.0)
            )
            if dump_leg:
                delta -= 5.0
                triggers.append(f"vwap_oversold_dump_leg_soft_{vdev_f:.2f}atr")
            elif short_prep:
                delta -= 8.0
                triggers.append(f"vwap_oversold_fade_prep_soft_{vdev_f:.2f}atr")
            else:
                blocks.append(f"vwap_oversold_{vdev_f:.2f}atr")
        if obv_rising is False and pos_in_range >= 0.70:
            delta += 8.0
            triggers.append("obv_distribution_at_top")
    else:
        if adx >= ADX_TREND_MIN and minus > 0 and minus > plus * DI_DOMINANCE:
            if pump_prep:
                delta -= 8.0
                triggers.append(f"adx_downtrend_pump_prep_soft_{adx:.0f}")
            elif adx >= adx_block:
                blocks.append(f"adx1h_downtrend_{adx:.0f}")
            else:
                delta -= 15.0
                triggers.append("adx1h_downtrend_against_long")
        if st_dir == -1:
            delta -= 8.0
            triggers.append("headwind_supertrend_1h_down")
        if vdev is not None and float(vdev) >= vwap_extreme:
            if pump_prep and pos_in_range <= 0.45:
                delta -= 5.0
                triggers.append(f"vwap_stretched_pump_prep_soft_{float(vdev):.2f}atr")
            else:
                blocks.append(f"vwap_overbought_{float(vdev):.2f}atr")
        if (
            phase == "accumulation"
            and chg_24h_pct is not None
            and float(chg_24h_pct) < -8.0
            and pos_in_range < 0.45
        ):
            delta -= 15.0
            triggers.append(
                f"weak_accumulation_soft_chg{float(chg_24h_pct):.0f}_pos{pos_in_range:.2f}"
            )
        if obv_rising is True and pos_in_range <= 0.30:
            delta += 8.0
            triggers.append("obv_accumulation_at_low")
    return delta, triggers, blocks


def hard_filter_blocks(
    blocks: list[Any],
    *,
    direction: str,
    phase: str,
    fall_from_high_pct: float = 0.0,
) -> list[str]:
    """Phase-aware filter severity for VWAP/ADX blocks."""
    out: list[str] = []
    for raw in blocks:
        tag = str(raw)
        if direction == "long" and phase in PUMP_PHASES_LONG and (
            tag.startswith("vwap_overbought") or tag.startswith("adx1h_uptrend")
        ):
            continue
        if direction == "short" and tag.startswith("adx1h_uptrend"):
            if phase in FADE_PHASES_SHORT or phase in DUMP_CONTINUATION_PHASES:
                continue
        if direction == "short" and tag.startswith("vwap_oversold"):
            if phase in {"dump_active", "distribution"}:
                continue
            if phase == "impulse_initiating" and fall_from_high_pct >= 8.0:
                continue
        out.append(tag)
    return out


_hard_filter_blocks = hard_filter_blocks

__all__ = ["directional_filters", "hard_filter_blocks"]
