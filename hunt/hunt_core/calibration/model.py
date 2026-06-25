"""P(win) accessor — reads the fusion engine's calibrated confidence."""
from __future__ import annotations

from typing import Any, Mapping


def predict_p_win(
    setup: Mapping[str, Any],
    *,
    direction: str = "",
    structure: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Read fusion_strength (preferred) or legacy p_win; NOT calibrated P(win)."""
    raw = setup.get("fusion_strength") if setup.get("fusion_strength") is not None else setup.get("p_win")
    try:
        p = float(raw) if raw is not None else None
    except (TypeError, ValueError):
        p = None
    return {"p_win": p, "source": "fusion"}
