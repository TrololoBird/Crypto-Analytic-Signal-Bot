"""Scanner routing — route_tick + delivery mode (P7)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

DetectorPath = Literal["short_dump", "long_bounce", "early_advisory", "none"]


@dataclass(frozen=True, slots=True)
class SetupCandidate:
    path: DetectorPath
    direction: str
    setup: dict[str, Any]
    row: dict[str, Any]
    lifecycle: dict[str, Any]


def route_tick(row: dict[str, Any]) -> list[SetupCandidate]:
    """Return candidate setups for this tick (H-B: all active paths)."""
    lifecycle = row.get("lifecycle") or {}
    if not isinstance(lifecycle, dict):
        lifecycle = {}
    out: list[SetupCandidate] = []
    dump = row.get("dump") or {}
    long_b = row.get("long") or {}
    if isinstance(dump, dict) and (dump.get("confirmed") or dump.get("score")):
        out.append(
            SetupCandidate(
                path="short_dump",
                direction="short",
                setup=dump,
                row=row,
                lifecycle=lifecycle,
            )
        )
    if isinstance(long_b, dict) and (long_b.get("confirmed") or long_b.get("score")):
        out.append(
            SetupCandidate(
                path="long_bounce",
                direction="long",
                setup=long_b,
                row=row,
                lifecycle=lifecycle,
            )
        )
    phase = str(lifecycle.get("phase") or "")
    if phase in {"impulse_initiating", "post_dump_bounce", "distribution"}:
        out.append(
            SetupCandidate(
                path="early_advisory",
                direction=str(lifecycle.get("recommended_bias") or "short"),
                setup={"phase": phase, "advisory": True},
                row=row,
                lifecycle=lifecycle,
            )
        )
    return out


DeliveryMode = Literal["monitor_only", "armed_first", "confirm_first"]

_FORMING_PHASES = frozenset(
    {
        "dump_setup_forming",
        "long_setup_forming",
        "dump_initiating",
        "long_initiating",
        "dump_imminent",
        "long_imminent",
        "exhaustion_watch",
        "accumulation_watch",
    }
)
_ARMED_PHASES = frozenset(
    {
        "dump_active",
        "distribution",
        "post_dump_bounce",
        "accumulation",
        "long_active",
        "squeeze",
        "impulse_active",
    }
)


def resolve_delivery_mode(
    lifecycle: dict[str, Any],
    setup: dict[str, Any],
) -> DeliveryMode:
    """How delivery tier routing treats forming vs confirmed setups."""
    if setup.get("confirmed"):
        return "confirm_first"
    phase = str(lifecycle.get("phase") or setup.get("lifecycle_phase") or "")
    if phase in _ARMED_PHASES:
        return "armed_first"
    fuel = float(
        setup.get("dump_fuel")
        or setup.get("long_fuel")
        or setup.get("dump_score")
        or setup.get("long_score")
        or 0
    )
    if phase in _FORMING_PHASES and fuel >= 45:
        return "armed_first"
    if phase in {"", "no_dump_yet", "no_long_yet", "impulse_initiating"}:
        return "monitor_only"
    return "monitor_only" if fuel < 45 else "armed_first"


__all__ = [
    "DeliveryMode",
    "DetectorPath",
    "SetupCandidate",
    "resolve_delivery_mode",
    "route_tick",
]
