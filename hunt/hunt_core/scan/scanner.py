"""Scanner orchestration — public detect API (P7 cutover)."""
from __future__ import annotations

import os

from hunt_core.scan.predump import (
    confirm_dump,
    enrich_dump_setup,
    phase_dump,
)
from hunt_core.scan.prepump import (
    confirm_long,
    enrich_long_setup,
    phase_long,
)
from hunt_core.scan.early import evaluate_early_alert
from hunt_core.scan.routing import (
    DeliveryMode,
    SetupCandidate,
    resolve_delivery_mode,
    route_tick,
)

HUNT_SCANNER_V2 = os.getenv("HUNT_SCANNER_V2", "").strip().lower() in {"1", "true", "yes"}


def route_tick_v2(row: dict) -> list[SetupCandidate]:
    """When HUNT_SCANNER_V2=1, EARLY hits are advisory-only until bar close."""
    hits = route_tick(row)
    if not HUNT_SCANNER_V2:
        return hits
    out: list[SetupCandidate] = []
    for hit in hits:
        mode = getattr(hit, "delivery_mode", None) or getattr(hit, "mode", "")
        if str(mode).lower() == "early":
            hit.advisory_only = True  # type: ignore[attr-defined]
        out.append(hit)
    return out


__all__ = [
    "DeliveryMode",
    "HUNT_SCANNER_V2",
    "SetupCandidate",
    "confirm_dump",
    "confirm_long",
    "enrich_dump_setup",
    "enrich_long_setup",
    "evaluate_early_alert",
    "phase_dump",
    "phase_long",
    "resolve_delivery_mode",
    "route_tick",
    "route_tick_v2",
]
