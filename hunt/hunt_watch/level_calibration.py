"""Adaptive SL/entry caps from 24h range + leg gain (parabolic memecoin mode).

Calibrated from hunt tracker outcomes (2026-06-10):
- stop_hit losses cluster ~3–5% SL dist on score 60–69
- tp2 wins (PLAY +15%) used tighter structural anchors
- parabolic legs (range>120%) need wider nominal SL cap OR local pivot
  anchor — not impulse wick high (VELVET 11% veto lesson)
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from functools import lru_cache
from typing import Literal

LevelMode = Literal["normal", "hot", "parabolic"]

# Defaults (overridden by hunt/data/hunt_calibration.json after calibrate_levels.py).
_SL_MAX_NORMAL = 8.0
_SL_MAX_HOT = 11.0
_SL_MAX_PARABOLIC = 14.0
_HOT_RANGE_PCT = 60.0
_PARABOLIC_RANGE_PCT = 120.0
_PARABOLIC_LEG_GAIN_PCT = 80.0

_FADE_SL_PHASES = frozenset({"exhaustion_at_high", "distribution"})
_BOUNCE_SL_PHASES = frozenset({"post_dump_bounce", "recovery", "accumulation"})
_PUMP_LONG_SL_PHASES = frozenset({"impulse_initiating", "breakout_arming"})


def _apply_phase_sl_atr(phase: str, params: AdaptiveLevelParams) -> AdaptiveLevelParams:
    """Q14: distribution fade 2.25×ATR cap; bounce long floor 2.0×ATR on 15m Wilder."""
    p = str(phase or "").strip()
    out = params
    if p and params.mode != "parabolic":
        atr = params.sl_max_atr
        if p in _FADE_SL_PHASES:
            atr = min(atr, 2.25)
        elif p in _BOUNCE_SL_PHASES:
            atr = max(2.0, min(atr, 2.25))
        if atr != params.sl_max_atr:
            out = replace(out, sl_max_atr=atr)
    pct = out.sl_max_pct
    if p in _PUMP_LONG_SL_PHASES:
        if out.mode == "hot":
            pct = round(min(17.0, pct + 5.0), 2)
        elif out.mode == "parabolic":
            pct = round(min(18.0, pct + 2.0), 2)
    elif p in _BOUNCE_SL_PHASES and out.mode == "hot":
        pct = round(min(14.0, pct + 2.0), 2)
    if pct != out.sl_max_pct:
        out = replace(out, sl_max_pct=pct)
    return out


@lru_cache(maxsize=1)
def _load_calibrated_caps() -> dict[str, float]:
    from hunt_watch.param_store import levels_thresholds, load_calibration

    lv = levels_thresholds()
    cal = load_calibration().get("outcome_calibration") or {}
    merged = {k: float(v) for k, v in cal.items() if isinstance(v, (int, float))}
    for key in (
        "sl_max_pct_normal",
        "sl_max_pct_parabolic",
        "sl_max_pct_hot",
        "hot_range_pct",
        "parabolic_range_pct",
        "parabolic_leg_gain_pct",
    ):
        if key in lv:
            merged[key] = float(lv[key])
    return merged


@dataclass(frozen=True, slots=True)
class AdaptiveLevelParams:
    mode: LevelMode
    sl_max_pct: float
    sl_max_atr: float
    sl_tp2_cap_ratio: float
    use_local_pivot_only: bool


def adaptive_level_params(
    *,
    range_pct_24h: float = 0.0,
    leg_gain_pct: float = 0.0,
    fall_from_high_pct: float = 0.0,
    symbol: str = "",
    lifecycle_phase: str = "",
) -> AdaptiveLevelParams:
    """Derive per-symbol level caps from session volatility."""
    from hunt_watch.param_store import levels_thresholds

    sym_lv = levels_thresholds(symbol) if symbol else {}
    caps = _load_calibrated_caps()
    normal_cap = sym_lv.get("sl_max_pct_normal", caps.get("sl_max_pct_normal", _SL_MAX_NORMAL))
    para_cap = sym_lv.get("sl_max_pct_parabolic", caps.get("sl_max_pct_parabolic", _SL_MAX_PARABOLIC))
    hot_cap = sym_lv.get("sl_max_pct_hot", min(_SL_MAX_HOT, round(normal_cap + 2.5, 2)))
    hot_range = sym_lv.get("hot_range_pct", caps.get("hot_range_pct", _HOT_RANGE_PCT))
    para_range = sym_lv.get("parabolic_range_pct", caps.get("parabolic_range_pct", _PARABOLIC_RANGE_PCT))
    para_leg = sym_lv.get("parabolic_leg_gain_pct", caps.get("parabolic_leg_gain_pct", _PARABOLIC_LEG_GAIN_PCT))

    rng = max(0.0, float(range_pct_24h))
    leg = max(0.0, float(leg_gain_pct))

    if rng >= para_range or leg >= para_leg:
        extra = min(6.0, max(0.0, rng - 50.0) * 0.028)
        base = min(normal_cap, 8.0)
        # Mid-dump on parabolic leg: use full para cap (BEAT −19% off high vetoed at 9.8% SL).
        sl_cap = para_cap if fall_from_high_pct >= 12.0 else min(para_cap, base + extra)
        return _apply_phase_sl_atr(
            lifecycle_phase,
            AdaptiveLevelParams(
            mode="parabolic",
            sl_max_pct=round(sl_cap, 2),
            sl_max_atr=2.0,
            sl_tp2_cap_ratio=0.45,
            use_local_pivot_only=True,
            ),
        )
    if rng >= hot_range or leg >= 40.0:
        extra = min(3.0, max(0.0, rng - hot_range) * 0.04)
        return _apply_phase_sl_atr(
            lifecycle_phase,
            AdaptiveLevelParams(
            mode="hot",
            sl_max_pct=round(min(hot_cap, normal_cap + extra), 2),
            sl_max_atr=2.25,
            sl_tp2_cap_ratio=0.5,
            use_local_pivot_only=fall_from_high_pct < 5.0 and leg >= 30.0,
            ),
        )
    return _apply_phase_sl_atr(
        lifecycle_phase,
        AdaptiveLevelParams(
        mode="normal",
        sl_max_pct=normal_cap,
        sl_max_atr=2.5,
        sl_tp2_cap_ratio=0.5,
        use_local_pivot_only=False,
        ),
    )


def calibrate_from_outcomes(closed: list[dict]) -> dict[str, float]:
    """Suggest SL_MAX_PCT from closed signals with known pnl (offline calibration)."""
    wins = [r for r in closed if r.get("close_reason") in {"tp1", "tp2"} and r.get("pnl_pct")]
    losses = [r for r in closed if r.get("close_reason") == "stop_hit" and r.get("pnl_pct")]
    if not losses:
        return {"sl_max_pct_normal": 8.0, "sl_max_pct_parabolic": 14.0}
    loss_pnls = [abs(float(r["pnl_pct"])) for r in losses]
    med_loss = sorted(loss_pnls)[len(loss_pnls) // 2]
    # Nominal cap slightly above median stop loss so viable setups pass when structure is tight.
    normal_cap = round(min(10.0, max(7.5, med_loss * 1.35)), 1)
    para_cap = round(min(15.0, normal_cap + 4.0), 1)
    win_avg = (
        sum(float(r["pnl_pct"]) for r in wins) / len(wins) if wins else 0.0
    )
    return {
        "sl_max_pct_normal": normal_cap,
        "sl_max_pct_parabolic": para_cap,
        "median_stop_loss_pct": round(med_loss, 2),
        "avg_win_pnl": round(win_avg, 2),
        "n_wins": len(wins),
        "n_stops": len(losses),
    }
