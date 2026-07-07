"""Setup catalog — legacy stubs for gate imports. All detectors removed;
only manipulation detection (scanner/detect/manipulation.py) remains."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Literal

Direction = Literal["short", "long"]


@dataclass(frozen=True, slots=True)
class SetupEvidence:
    setup_id: str
    direction: Direction
    strength: float
    confirmed: bool
    reasons: tuple[str, ...] = ()
    entry: float = 0.0
    stop_loss: float = 0.0
    tp1: float = 0.0
    tp2: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)


_CATALOG_SETUP_TYPE: dict[str, str] = {}


def catalog_struct_setup_type(setup_id: str | None) -> str | None:
    sid = str(setup_id or "").strip()
    if not sid:
        return None
    return _CATALOG_SETUP_TYPE.get(sid)


def legacy_fuel_merge_enabled() -> bool:
    env = os.environ.get("HUNT_LEGACY_FUEL")
    if env is not None:
        return env.strip().lower() in {"1", "true", "yes"}
    from hunt_core.params.store import universal_section
    dl = universal_section("delivery")
    return not bool(dl.get("ev_primary_default", True))


__all__ = [
    "SetupEvidence",
    "catalog_struct_setup_type",
    "legacy_fuel_merge_enabled",
]
