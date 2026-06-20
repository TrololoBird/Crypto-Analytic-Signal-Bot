"""Thin tick router for the fusion engine (replaces scan/routing.py).

The fusion ``confirmed`` flag already encodes the full decision — gate cleared *and* a
matching PRE phase (``gate_open = gate AND watch_ok``). So routing is trivial: a confirmed
side is a delivery candidate. No forming/armed/anticipation tiers, no legacy phase
vocabulary.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

DeliveryMode = Literal["monitor_only", "confirm_first"]


@dataclass
class SetupCandidate:
    path: str
    direction: str
    setup: dict[str, Any]
    row: dict[str, Any]
    lifecycle: dict[str, Any]


def route_tick(row: dict[str, Any]) -> list[SetupCandidate]:
    """Confirmed fusion setups become delivery candidates (one direction per symbol)."""
    lifecycle = row.get("lifecycle") if isinstance(row.get("lifecycle"), dict) else {}
    dump = row.get("dump") or {}
    long_b = row.get("long") or {}
    out: list[SetupCandidate] = []
    for direction, base, path in (
        ("short", dump, "short_dump"),
        ("long", long_b, "long_bounce"),
    ):
        if isinstance(base, dict) and (base.get("confirmed") or base.get("intrabar_confirmed")):
            out.append(SetupCandidate(path, direction, base, row, lifecycle))
    return out


def resolve_delivery_mode(lifecycle: dict[str, Any], setup: dict[str, Any]) -> DeliveryMode:
    """Confirmed setups go to the confirm lane; everything else is monitor-only."""
    if setup.get("confirmed") or setup.get("intrabar_confirmed"):
        return "confirm_first"
    return "monitor_only"


__all__ = ["DeliveryMode", "SetupCandidate", "resolve_delivery_mode", "route_tick"]
