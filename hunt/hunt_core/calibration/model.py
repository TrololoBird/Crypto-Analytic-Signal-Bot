"""P(win) accessor — reads the fusion engine's calibrated confidence."""
from __future__ import annotations

from typing import Any, Mapping


def predict_p_win(
    setup: Mapping[str, Any],
    *,
    direction: str = "",
    structure: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Fusion p_win is the calibrated win probability; no separate shadow model."""
    try:
        p = float(setup.get("p_win")) if setup.get("p_win") is not None else None
    except (TypeError, ValueError):
        p = None
    return {"p_win": p, "source": "fusion"}
