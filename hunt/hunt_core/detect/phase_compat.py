"""Fusion ↔ legacy lifecycle phase vocabulary for delivery gates.

Post-fusion (2026-06-20) the tick writer sets ``lifecycle.phase`` from CUSUM
(``pre_pump`` / ``pre_dump`` / ``mid`` / ``neutral``). Mission, sniper, and
lifecycle gates still referenced the removed 10-state FSM — this module is the
single source of truth for phase sets used on the delivery path.
"""
from __future__ import annotations

from typing import Any

from hunt_core.detect.phase import MID, NEUTRAL, PRE_DUMP, PRE_PUMP

FUSION_PHASES = frozenset({PRE_PUMP, PRE_DUMP, MID, NEUTRAL})

LEGACY_PRE_DUMP_PHASES = frozenset(
    {
        "exhaustion_at_high",
        "distribution",
        "dump_initiating",
    }
)
LEGACY_PRE_PUMP_PHASES = frozenset(
    {
        "accumulation",
        "breakout_arming",
        "post_dump_bounce",
        "recovery",
    }
)
LEGACY_MID_LEG_PHASES = frozenset(
    {
        "dump_active",
        "impulse_initiating",
        "mega_leg_continuation",
    }
)

PRE_DUMP_MISSION_PHASES = LEGACY_PRE_DUMP_PHASES | frozenset({PRE_DUMP})
PRE_PUMP_MISSION_PHASES = LEGACY_PRE_PUMP_PHASES | frozenset({PRE_PUMP})
MID_LEG_PHASES = LEGACY_MID_LEG_PHASES | frozenset({MID})


def is_fusion_phase(phase: str) -> bool:
    return str(phase or "") in FUSION_PHASES


def fusion_lifecycle_flags(
    *,
    side: str,
    phase: str,
    gate_open: bool,
    watch_ok: bool,
) -> dict[str, bool]:
    """Entry/confirm flags for lifecycle gates derived from fusion detection."""
    p = str(phase or "")
    s = str(side or "")
    pre_short = s == "short" and p == PRE_DUMP and watch_ok
    pre_long = s == "long" and p == PRE_PUMP and watch_ok
    return {
        "short_entry_ok": s == "short" and (gate_open or pre_short),
        "long_entry_ok": s == "long" and (gate_open or pre_long),
        "short_confirm_ok": s == "short" and watch_ok and p not in {MID, NEUTRAL},
        "long_confirm_ok": s == "long" and watch_ok and p not in {MID, NEUTRAL},
    }


def fusion_lifecycle_dict(
    detection: Any,
    *,
    structure_bias: str,
    fall_from_high_pct: float,
    leg_gain_pct: float,
) -> dict[str, Any]:
    """Build ``row["lifecycle"]`` from a fusion ``Detection`` (or neutral stub)."""
    if detection is None:
        return {
            "phase": NEUTRAL,
            "phase_fusion": NEUTRAL,
            "bias": "",
            "recommended_bias": "",
            "structure_bias": structure_bias,
            "invalidate_short": False,
            "fall_from_high_pct": fall_from_high_pct,
            "leg_gain_pct": leg_gain_pct,
            "watch_ok": False,
            "short_entry_ok": False,
            "long_entry_ok": False,
            "short_confirm_ok": False,
            "long_confirm_ok": False,
            "cusum": 0.0,
            "cusum_band": 0.0,
        }

    side = detection.side if detection.side in {"long", "short"} else ""
    phase_val = str(detection.phase or NEUTRAL)
    flags = fusion_lifecycle_flags(
        side=side,
        phase=phase_val,
        gate_open=bool(detection.gate_open),
        watch_ok=bool(detection.watch_ok),
    )
    pi = detection.phase_info
    band = pi.band
    return {
        "phase": phase_val,
        "phase_fusion": phase_val,
        "bias": side,
        "recommended_bias": side,
        "structure_bias": structure_bias,
        "invalidate_short": False,
        "fall_from_high_pct": fall_from_high_pct,
        "leg_gain_pct": leg_gain_pct,
        "watch_ok": bool(detection.watch_ok),
        "cusum": round(float(pi.cusum), 4),
        "cusum_band": round(float(band), 4) if band is not None else None,
        **flags,
    }


__all__ = [
    "FUSION_PHASES",
    "LEGACY_MID_LEG_PHASES",
    "LEGACY_PRE_DUMP_PHASES",
    "LEGACY_PRE_PUMP_PHASES",
    "MID_LEG_PHASES",
    "PRE_DUMP_MISSION_PHASES",
    "PRE_PUMP_MISSION_PHASES",
    "fusion_lifecycle_dict",
    "fusion_lifecycle_flags",
    "is_fusion_phase",
]
