"""Lifecycle dict normalization and shared delivery vetoes."""
from __future__ import annotations

from typing import Any

from hunt_core.gate._types import GateResult


def lifecycle_dict(lifecycle: Any | None) -> dict[str, Any]:
    if isinstance(lifecycle, dict):
        return lifecycle
    if lifecycle is None:
        return {}
    return {
        "phase": lifecycle.phase.value,
        "recommended_bias": lifecycle.recommended_bias,
        "fall_from_high_pct": lifecycle.fall_from_high_pct,
        "bounce_from_low_pct": lifecycle.bounce_from_low_pct,
        "short_entry_ok": lifecycle.short_entry_ok,
        "short_confirm_ok": lifecycle.short_confirm_ok,
        "invalidate_short": lifecycle.invalidate_short,
    }


def lifecycle_veto_hard(setup: dict[str, Any]) -> GateResult | None:
    for raw in setup.get("confirm_hard") or []:
        tag = str(raw)
        if tag.startswith("veto_lifecycle") or tag.startswith("veto_mtf"):
            label = "mtf_veto_hard" if tag.startswith("veto_mtf") else "lifecycle_veto_hard"
            return GateResult(False, label, f"Confirm veto: {tag}")
    return None


def bias_conflict(direction: str, lc: dict[str, Any]) -> GateResult | None:
    bias = str(lc.get("recommended_bias") or "")
    if direction == "short" and bias == "long":
        return GateResult(False, "bias_conflict", "Bias long — открытый шорт против lifecycle")
    if direction == "long" and bias == "short":
        return GateResult(False, "bias_conflict", "Bias short — открытый лонг против lifecycle")
    return None


def core_lifecycle_blockers(
    setup: dict[str, Any],
    *,
    direction: str,
    lc: dict[str, Any],
) -> GateResult | None:
    """Shared lifecycle vetoes for delivery and /signals report parity."""
    phase = str(lc.get("phase") or "")
    if phase == "no_setup":
        return GateResult(False, "stale_no_setup", "Lifecycle no_setup — сетап исчез")
    bias_hit = bias_conflict(direction, lc)
    if bias_hit is not None:
        return bias_hit
    veto = lifecycle_veto_hard(setup)
    if veto is not None:
        return veto
    return None


_lifecycle_dict = lifecycle_dict
_lifecycle_veto_hard = lifecycle_veto_hard
_bias_conflict = bias_conflict
_core_lifecycle_blockers = core_lifecycle_blockers

__all__ = [
    "bias_conflict",
    "core_lifecycle_blockers",
    "lifecycle_dict",
    "lifecycle_veto_hard",
]
