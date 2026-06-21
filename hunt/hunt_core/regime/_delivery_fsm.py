"""Per-symbol delivery stage FSM (forming → armed → triggered → active)."""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Literal

Direction = Literal["short", "long"]


def _resolve_state(state: Any | None) -> Any:
    from hunt_core.runtime.state import current_symbol_state

    return state or current_symbol_state()




class DeliveryStage(StrEnum):
    FORMING = "forming"
    ARMED = "armed"
    TRIGGERED = "triggered"
    ACTIVE = "active"
    RESOLVED = "resolved"


_STAGE_ORDER: tuple[DeliveryStage, ...] = (
    DeliveryStage.FORMING,
    DeliveryStage.ARMED,
    DeliveryStage.TRIGGERED,
    DeliveryStage.ACTIVE,
    DeliveryStage.RESOLVED,
)

_ALLOWED: dict[DeliveryStage, frozenset[DeliveryStage]] = {
    DeliveryStage.FORMING: frozenset({DeliveryStage.ARMED, DeliveryStage.TRIGGERED, DeliveryStage.RESOLVED}),
    DeliveryStage.ARMED: frozenset({DeliveryStage.TRIGGERED, DeliveryStage.FORMING, DeliveryStage.RESOLVED}),
    DeliveryStage.TRIGGERED: frozenset({DeliveryStage.ACTIVE, DeliveryStage.ARMED, DeliveryStage.RESOLVED}),
    DeliveryStage.ACTIVE: frozenset({DeliveryStage.RESOLVED, DeliveryStage.TRIGGERED}),
    DeliveryStage.RESOLVED: frozenset({DeliveryStage.FORMING}),
}


@dataclass(frozen=True, slots=True)
class DeliveryFsmState:
    stage: DeliveryStage
    direction: str
    setup_id: str
    transitioned: bool
    previous: DeliveryStage | None


@dataclass(slots=True)
class _FsmEntry:
    stage: str = DeliveryStage.FORMING.value
    direction: str = ""
    setup_id: str = ""


def _key(symbol: str, direction: str) -> str:
    return f"{symbol.upper()}:{direction.lower()}"


def _infer_target(
    setup: dict[str, Any],
    *,
    direction: str = "short",
    delivery_tier: str | None = None,
    tracker_active: bool = False,
    tracker_closed: bool = False,
) -> DeliveryStage:
    if tracker_closed:
        return DeliveryStage.RESOLVED
    if tracker_active:
        return DeliveryStage.ACTIVE
    tier = str(delivery_tier or "").upper()
    if tier == "TRIGGERED" or bool(setup.get("confirmed")):
        return DeliveryStage.TRIGGERED if tier != "ACTIVE" else DeliveryStage.ACTIVE
    if tier == "ARMED":
        return DeliveryStage.ARMED
    from hunt_core.gate._ev import setup_conviction_pct

    if setup_conviction_pct(setup, direction=direction) > 0 or setup.get("phase"):
        return DeliveryStage.FORMING
    return DeliveryStage.FORMING


def advance_delivery_fsm(
    symbol: str,
    direction: Direction,
    setup: dict[str, Any] | None,
    *,
    delivery_tier: str | None = None,
    tracker_active: bool = False,
    tracker_closed: bool = False,
    setup_id: str = "",
    state: Any | None = None,
) -> DeliveryFsmState | None:
    """Advance per-symbol delivery FSM; returns None when setup empty."""
    if not setup:
        return None
    store = _resolve_state(state)
    sym = symbol.upper()
    d = direction.lower()
    k = _key(sym, d)
    entry = store.delivery_fsm.setdefault(k, _FsmEntry())
    if not isinstance(entry, _FsmEntry):
        entry = _FsmEntry()
        store.delivery_fsm[k] = entry

    try:
        prev = DeliveryStage(entry.stage)
    except ValueError:
        prev = DeliveryStage.FORMING

    target = _infer_target(
        setup,
        direction=d,
        delivery_tier=delivery_tier,
        tracker_active=tracker_active,
        tracker_closed=tracker_closed,
    )
    sid = setup_id or str(setup.get("setup_id") or setup.get("phase") or "unknown")
    entry.direction = d
    entry.setup_id = sid

    if target == prev:
        return DeliveryFsmState(
            stage=prev,
            direction=d,
            setup_id=sid,
            transitioned=False,
            previous=None,
        )

    allowed = _ALLOWED.get(prev, frozenset())
    if target not in allowed and not (prev == DeliveryStage.FORMING and target == DeliveryStage.TRIGGERED):
        return DeliveryFsmState(
            stage=prev,
            direction=d,
            setup_id=sid,
            transitioned=False,
            previous=None,
        )

    entry.stage = target.value
    return DeliveryFsmState(
        stage=target,
        direction=d,
        setup_id=sid,
        transitioned=True,
        previous=prev,
    )


def record_delivery_fsm(
    symbol: str,
    direction: Direction,
    setup: dict[str, Any] | None,
    *,
    delivery_tier: str | None = None,
    tracker_active: bool = False,
    tracker_closed: bool = False,
    setup_id: str = "",
    state: Any | None = None,
) -> DeliveryFsmState | None:
    """Advance FSM and append funnel telemetry when stage changes."""
    fsm = advance_delivery_fsm(
        symbol,
        direction,
        setup,
        delivery_tier=delivery_tier,
        tracker_active=tracker_active,
        tracker_closed=tracker_closed,
        setup_id=setup_id,
        state=state,
    )
    if fsm is None or not fsm.transitioned:
        return fsm
    from hunt_core.track.events import record_phase_transition

    record_phase_transition(
        symbol=symbol,
        direction=direction,
        from_phase=fsm.previous.value if fsm.previous else "",
        to_phase=fsm.stage.value,
        detail=f"delivery_fsm:{fsm.setup_id}",
        payload={
            "fsm": "delivery",
            "setup_id": fsm.setup_id,
            "delivery_tier": delivery_tier,
        },
    )
    return fsm


# Mid-file __all__ removed (P4) — full public API exported via detect/lifecycle shim.


__all__ = [
    "DeliveryFsmState",
    "DeliveryStage",
    "advance_delivery_fsm",
    "record_delivery_fsm",
]
