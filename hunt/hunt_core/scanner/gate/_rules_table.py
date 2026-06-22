"""Declarative delivery gate rule table (Phase 6)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

DeliveryGateTier = Literal["armed", "triggered", "both"]


@dataclass(frozen=True, slots=True)
class GateRule:
    """Ordered declarative delivery gate — ``check_fn`` names a policy check handler."""

    id: str
    check_fn: str
    block_code: str
    required_for_tier: DeliveryGateTier


DELIVERY_GATE_RULES: tuple[GateRule, ...] = (
    GateRule("data_complete", "_decl_check_data_complete", "data_incomplete", "both"),
    GateRule("data_stale", "_decl_check_data_stale", "data_stale", "both"),
    GateRule(
        "structure_aligned",
        "_decl_check_structure_aligned",
        "structure_bias_conflict",
        "both",
    ),
    GateRule(
        "lifecycle_context",
        "_decl_check_lifecycle_context",
        "lifecycle_context_veto",
        "both",
    ),
    GateRule("at_level", "_decl_check_at_level", "not_at_level", "armed"),
    GateRule("ignition_floor", "_decl_check_ignition_floor", "ignition_low", "armed"),
    GateRule("rr_floor", "_decl_check_rr_floor", "rr_below_min", "both"),
    GateRule("playbook", "_decl_check_playbook", "playbook_fail", "both"),
    GateRule("ev_delivery", "_decl_check_ev_delivery", "ev_incomplete", "both"),
    GateRule(
        "structural_trigger",
        "_decl_check_structural_trigger",
        "no_structural_trigger",
        "triggered",
    ),
    GateRule(
        "orderflow_present",
        "_decl_check_orderflow_present",
        "orderflow_misaligned",
        "triggered",
    ),
    GateRule("setup_type", "_decl_check_setup_type", "no_setup_type", "triggered"),
    GateRule("meme_anomaly", "_decl_check_meme_anomaly", "not_anomaly", "both"),
    GateRule(
        "meme_pump_volume",
        "_decl_check_meme_pump_volume",
        "meme_pump_volume_low",
        "both",
    ),
    GateRule("delivery_confluence", "_decl_check_delivery_confluence", "delivery_confluence_low", "triggered"),
    GateRule("exhaustion_fade", "_decl_check_exhaustion_fade", "exhaustion_fade_weak", "triggered"),
    GateRule("impulse_long", "_decl_check_impulse_long", "impulse_session_weak", "triggered"),
    GateRule("accumulation_long", "_decl_check_accumulation_long", "accumulation_long_weak", "triggered"),
    GateRule("wash_baseline", "_decl_check_wash_baseline", "wash_no_baseline", "both"),
)


__all__ = ["DELIVERY_GATE_RULES", "DeliveryGateTier", "GateRule"]
