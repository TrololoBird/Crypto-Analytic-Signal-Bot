"""X1-B model shadow stub — rule-based EV only until calibration labels exist."""
from __future__ import annotations

from typing import Any, Mapping


def model_shadow_score(
    setup: Mapping[str, Any],
    *,
    direction: str,
    structure: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return shadow model output; delegates to rule-based EV for now."""
    from hunt_core.contract import compute_rule_based_ev

    ev = compute_rule_based_ev(setup, direction=direction, structure=structure)
    return {
        "model": "rule_shadow_v0",
        "p_win": ev.get("p_win"),
        "ev": ev.get("ev"),
        "reason": ev.get("reason"),
    }
