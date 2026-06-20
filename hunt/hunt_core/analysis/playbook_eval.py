"""N-of-M playbook gates — boolean checklist per archetype (anti-heuristic delivery)."""
from __future__ import annotations

from typing import Any, Literal

from hunt_core.analysis.playbook_checks import (
    playbook_pass_count,
    playbook_passes,
)


def playbook_passes_row(row: dict[str, Any]) -> bool:
    fusion = row.get("manipulation_fusion")
    if not isinstance(fusion, dict):
        return False
    archetype = str(fusion.get("archetype") or "none")
    checks = fusion.get("checks") if isinstance(fusion.get("checks"), dict) else {}
    return playbook_passes(archetype, checks)


def setup_meets_playbook(
    setup: dict[str, Any],
    *,
    row: dict[str, Any] | None = None,
    direction: Literal["short", "long"] = "short",
) -> bool:
    """Direction-aware playbook gate for delivery/advisory."""
    source_row = row or {}
    fusion = source_row.get("manipulation_fusion")
    if not isinstance(fusion, dict):
        return False
    archetype = str(fusion.get("archetype") or "none")
    if direction == "short" and archetype != "predump_short":
        return False
    if direction == "long" and archetype not in {"coil_long", "ignition_long"}:
        return False
    checks = fusion.get("checks") if isinstance(fusion.get("checks"), dict) else {}
    if not playbook_passes(archetype, checks):
        return False
    if direction == "short" and not checks.get("anti_squeeze", True):
        return False
    return bool(setup.get("confirmed") or setup.get("intrabar_confirmed"))


__all__ = [
    "playbook_pass_count",
    "playbook_passes",
    "playbook_passes_row",
    "setup_meets_playbook",
]
