"""Sticky expansion-state FSM (separate from the fusion CUSUM phase).

Smooths the per-tick derived state without hurting on-demand probes:

  * entering a directional state from a non-directional one (neutral / accumulation /
    distribution) is immediate — a fresh ``/expand`` shows the real state on tick one;
  * escalating within the same side (pre_pump → active_pump) is immediate;
  * *flipping to the opposite side* (pre_pump → pre_dump) requires two consecutive
    confirmations, so a single noisy tick cannot whipsaw the state;
  * dropping toward neutral is immediate.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass

from typing import Any

from hunt_core.analysis.expansion_engine.types import ExpansionStateKind

_CONFIRM_REQUIRED = 2
_DIRECTIONAL = {"pre_pump", "pre_dump", "active_pump", "active_dump"}
_PUMP_FAMILY = {"pre_pump", "active_pump"}
_DUMP_FAMILY = {"pre_dump", "active_dump"}


def _family(state: ExpansionStateKind) -> str:
    if state in _PUMP_FAMILY:
        return "pump"
    if state in _DUMP_FAMILY:
        return "dump"
    return "flat"


@dataclass(slots=True)
class _Sticky:
    state: ExpansionStateKind = "neutral"
    candidate: ExpansionStateKind = "neutral"
    streak: int = 0


class ExpansionStateMachine:
    def __init__(self) -> None:
        self._store: dict[str, _Sticky] = {}
        self._lock = threading.Lock()

    def transition(self, symbol: str, derived: ExpansionStateKind) -> ExpansionStateKind:
        sym = symbol.upper()
        with self._lock:
            st = self._store.setdefault(sym, _Sticky())
            if derived == st.candidate:
                st.streak += 1
            else:
                st.candidate = derived
                st.streak = 1

            if derived not in _DIRECTIONAL:
                # Non-directional states settle immediately.
                st.state = derived
            elif _family(st.state) != "dump" and _family(derived) == "pump":
                # Immediate entry/escalation into the pump family from flat or pump.
                st.state = derived
            elif _family(st.state) != "pump" and _family(derived) == "dump":
                # Immediate entry/escalation into the dump family from flat or dump.
                st.state = derived
            elif st.streak >= _CONFIRM_REQUIRED:
                # Opposite-family flip — only after confirmation.
                st.state = derived
            # else: hold previous state until confirmation count is met
            return st.state

    def clear(self, symbol: str | None = None) -> None:
        with self._lock:
            if symbol is None:
                self._store.clear()
            else:
                self._store.pop(symbol.upper(), None)

    def snapshot(self) -> dict[str, dict[str, Any]]:
        """Serialize sticky states for process restart."""
        with self._lock:
            return {
                sym: {
                    "state": st.state,
                    "candidate": st.candidate,
                    "streak": st.streak,
                }
                for sym, st in self._store.items()
            }

    def restore(self, data: dict[str, Any] | None) -> None:
        """Restore sticky states from :meth:`snapshot`."""
        if not isinstance(data, dict):
            return
        with self._lock:
            for sym, raw in data.items():
                if not isinstance(raw, dict):
                    continue
                state = str(raw.get("state") or "neutral")
                candidate = str(raw.get("candidate") or state)
                try:
                    streak = int(raw.get("streak") or 0)
                except (TypeError, ValueError):
                    streak = 0
                self._store[str(sym).upper()] = _Sticky(
                    state=state,  # type: ignore[arg-type]
                    candidate=candidate,  # type: ignore[arg-type]
                    streak=max(0, streak),
                )


_GLOBAL_FSM = ExpansionStateMachine()


def global_state_machine() -> ExpansionStateMachine:
    return _GLOBAL_FSM


__all__ = ["ExpansionStateMachine", "global_state_machine"]
