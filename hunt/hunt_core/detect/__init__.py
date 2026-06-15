"""Detection engine facade — P7 re-exports from scan/."""

from hunt_core.scan.scanner import (
    DeliveryMode,
    SetupCandidate,
    confirm_dump,
    confirm_long,
    enrich_dump_setup,
    enrich_long_setup,
    phase_dump,
    phase_long,
    resolve_delivery_mode,
    route_tick,
)

__all__ = [
    "DeliveryMode",
    "SetupCandidate",
    "confirm_dump",
    "confirm_long",
    "enrich_dump_setup",
    "enrich_long_setup",
    "phase_dump",
    "phase_long",
    "resolve_delivery_mode",
    "route_tick",
]


def __getattr__(name: str):
    if name in {
        "AdaptiveStore",
        "DeliveryStage",
        "HuntLifecycle",
        "HuntPhase",
        "assess_hunt_lifecycle",
        "evaluate_early_alert",
        "load_adaptive_store",
        "record_delivery_fsm",
        "reset_symbol",
        "save_adaptive_store",
        "stabilize",
    }:
        if name in {
            "DeliveryStage",
            "HuntLifecycle",
            "HuntPhase",
            "assess_hunt_lifecycle",
            "record_delivery_fsm",
            "reset_symbol",
            "stabilize",
        }:
            from hunt_core.regime import leg_fsm as _lifecycle

            return getattr(_lifecycle, name)
        from hunt_core.scan import _engine_impl as _engine

        return getattr(_engine, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
