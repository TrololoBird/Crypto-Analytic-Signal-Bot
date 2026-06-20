"""Scanner public API — fusion engine cutover shim.

Detection is owned by ``detect/*``; this module re-exports the tick router and
no-op stubs for legacy call sites that still import ``hunt_core.scan.scanner``.
"""
from __future__ import annotations

from typing import Any

from hunt_core.detect.routing import (
    DeliveryMode,
    SetupCandidate,
    resolve_delivery_mode,
    route_tick,
)

HUNT_SCANNER_V2 = False


def route_tick_v2(row: dict[str, Any]) -> list[SetupCandidate]:
    return route_tick(row)


def confirm_dump(*_a: Any, **_k: Any) -> bool:
    return False


def confirm_long(*_a: Any, **_k: Any) -> bool:
    return False


def enrich_dump_setup(setup: dict[str, Any], **_k: Any) -> dict[str, Any]:
    return setup


def enrich_long_setup(setup: dict[str, Any], **_k: Any) -> dict[str, Any]:
    return setup


def evaluate_early_alert(*_a: Any, **_k: Any) -> None:
    return None


def phase_dump(*_a: Any, **_k: Any) -> str:
    return ""


def phase_long(*_a: Any, **_k: Any) -> str:
    return ""


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
