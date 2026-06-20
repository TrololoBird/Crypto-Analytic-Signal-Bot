"""Calibrated P(win) model — shadow layer (X1-B)."""
from __future__ import annotations

from typing import Any, Mapping

from hunt_core.ev.model_shadow import model_shadow_score


def train_shadow_model(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Placeholder trainer — rule-based until deduped labels exist."""
    _ = rows
    return {"model": "rule_shadow_v0", "n": 0, "status": "stub"}


def predict_p_win(
    setup: Mapping[str, Any],
    *,
    direction: str,
    structure: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return model_shadow_score(setup, direction=direction, structure=structure)
