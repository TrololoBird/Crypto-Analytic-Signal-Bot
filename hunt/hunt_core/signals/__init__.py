"""Shared signal lifecycle spine — Module 1 + Module 2 emit through here."""
from hunt_core.signals.emit import SignalEmitter, emit_lifecycle_message
from hunt_core.signals.lifecycle import SignalLifecycleStore, compute_setup_id, process_lifecycle_tick
from hunt_core.signals.model import Signal, SignalModule, SignalState

__all__ = [
    "Signal",
    "SignalEmitter",
    "SignalLifecycleStore",
    "SignalModule",
    "SignalState",
    "compute_setup_id",
    "emit_lifecycle_message",
    "process_lifecycle_tick",
]
