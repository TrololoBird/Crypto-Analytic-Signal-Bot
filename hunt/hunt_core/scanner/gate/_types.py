"""Shared gate types and report ordering."""
from __future__ import annotations

from dataclasses import dataclass

BOUNCE_MIN_RISK_REWARD = 0.5


@dataclass(frozen=True, slots=True)
class GateResult:
    ok: bool
    code: str
    message: str


# Display order for /signals (most actionable first — independent of alert short-circuit).
REPORT_BLOCK_PRIORITY: dict[str, int] = {
    "stale_no_setup": 0,
    "invalidate_short": 1,
    "bias_conflict": 2,
    "structure_bias_conflict": 2,
    "not_at_level": 3,
    "short_entry_not_ok": 3,
    "long_blocked_mid_dump": 4,
    "long_below_resistance": 5,
    "long_below_hunt_high": 5,
    "lifecycle_veto_hard": 6,
    "below_forming_min": 7,
    "ignition_low": 9,
    "premature_exhaustion": 9,
    "not_confirmed": 10,
    "filter_block": 11,
    "not_anomaly": 12,
    "levels_veto": 13,
    "rr_below_min": 14,
    "ev_incomplete": 15,
    "ev_below_floor": 15,
    "confidence_score_missing": 15,
    "confidence_score_below_floor": 15,
    "tp2_too_close": 16,
    "delivery_confluence_low": 17,
    "data_missing_adx1h": 6,
    "data_missing_pos_in_range": 6,
    "exhaustion_fade_weak": 18,
    "accumulation_long_weak": 19,
    "exhaustion_strong_trend": 18,
    "impulse_session_weak": 19,
    "impulse_oi_weak": 19,
    "data_incomplete": 5,
    "wash_trading": 6,
    "wash_trading_wti": 6,
    "wash_trading_ms": 6,
    "wash_data_missing": 6,
    "kinematic_chase": 7,
    "kinematic_data_missing": 7,
    "mission_mid_dump": 1,
    "mission_mid_pump": 1,
    "mission_not_pre_dump": 1,
    "mission_not_pre_pump": 1,
    "price_stale": 2,
}


__all__ = ["BOUNCE_MIN_RISK_REWARD", "GateResult", "REPORT_BLOCK_PRIORITY"]
