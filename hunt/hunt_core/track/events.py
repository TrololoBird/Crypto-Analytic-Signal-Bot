"""Signal events log — hunt_core canonical (append-only lifecycle log)."""
from __future__ import annotations



import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from hunt_core.paths import SIGNAL_EVENTS

FUNNEL_STAGES: tuple[str, ...] = (
    "prescan",
    "lifecycle",
    "armed",
    "dump_initiation",
    "dump_active",
    "fuel",
    "wash",
    "tier",
    "deliver",
)

# Lifecycle phases that map to dedicated funnel telemetry stages (0a baseline).
_LIFECYCLE_FUNNEL_MAP: dict[str, str] = {
    "dump_initiating": "dump_initiation",
    "dump_active": "dump_active",
}


def append_signal_event(
    event: str,
    *,
    symbol: str,
    direction: str = "",
    detail: str = "",
    payload: dict[str, Any] | None = None,
    path: Path = SIGNAL_EVENTS,
) -> None:
    row = {
        "ts": datetime.now(UTC).isoformat(),
        "event": event,
        "symbol": symbol.upper(),
        "direction": direction.lower() if direction else "",
        "detail": detail,
        "payload": payload or {},
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, default=str) + "\n")


def record_funnel_stage(
    stage: str,
    *,
    symbol: str,
    direction: str = "",
    detail: str = "",
    payload: dict[str, Any] | None = None,
    path: Path = SIGNAL_EVENTS,
) -> None:
    """Telemetry funnel stage → signal_events JSONL (P0 telemetry)."""
    stage_norm = stage if stage in FUNNEL_STAGES else "unknown"
    body = {"stage": stage_norm, **(payload or {})}
    append_signal_event(
        f"funnel_{stage_norm}",
        symbol=symbol,
        direction=direction,
        detail=detail,
        payload=body,
        path=path,
    )


def record_lifecycle_funnel(
    *,
    symbol: str,
    phase: str,
    prev_phase: str | None = None,
    bias: str = "",
    payload: dict[str, Any] | None = None,
    path: Path = SIGNAL_EVENTS,
) -> None:
    """Record lifecycle transition + mapped anticipation funnel stages (0a baseline)."""
    body = {"phase": phase, "prev": prev_phase, "bias": bias, **(payload or {})}
    record_funnel_stage(
        "lifecycle",
        symbol=symbol,
        detail=phase,
        payload=body,
        path=path,
    )
    mapped = _LIFECYCLE_FUNNEL_MAP.get(phase)
    if mapped:
        record_funnel_stage(
            mapped,
            symbol=symbol,
            detail=phase,
            payload=body,
            path=path,
        )


def record_phase_transition(
    *,
    symbol: str,
    direction: str,
    from_phase: str,
    to_phase: str,
    detail: str = "",
    payload: dict[str, Any] | None = None,
    path: Path = SIGNAL_EVENTS,
) -> None:
    """Append tracker FSM phase transition to signal_events JSONL."""
    body = {
        "from_phase": from_phase,
        "to_phase": to_phase,
        **(payload or {}),
    }
    append_signal_event(
        "phase_transition",
        symbol=symbol,
        direction=direction,
        detail=detail or f"{from_phase}->{to_phase}",
        payload=body,
        path=path,
    )


__all__ = [
    "FUNNEL_STAGES",
    "append_audit_log",
    "append_signal_event",
    "audit_probe_row",
    "backtest_levels_on_bars",
    "record_funnel_stage",
    "record_lifecycle_funnel",
    "record_phase_transition",
]

# P10: signal_audit merged into events (canonical log path).
from hunt_core.track.signal_audit import (  # noqa: E402
    append_audit_log,
    audit_probe_row,
    backtest_levels_on_bars,
)
