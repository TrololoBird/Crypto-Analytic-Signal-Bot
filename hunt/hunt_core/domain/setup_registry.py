"""Hunt setup registry — canonical setup IDs (P7)."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from hunt_core.setups.catalog import HUNT_SETUP_IDS, HUNT_SETUP_META


@dataclass(frozen=True, slots=True)
class SetupEvidence:
    setup_id: str
    direction: str
    score: float
    triggers: tuple[str, ...] = ()
    meta: dict[str, Any] = field(default_factory=dict)


def registry_ids() -> tuple[str, ...]:
    return tuple(HUNT_SETUP_IDS)


def registry_meta(setup_id: str) -> dict[str, Any]:
    return dict(HUNT_SETUP_META.get(setup_id) or {})


__all__ = [
    "HUNT_SETUP_IDS",
    "HUNT_SETUP_META",
    "SetupEvidence",
    "registry_ids",
    "registry_meta",
]
