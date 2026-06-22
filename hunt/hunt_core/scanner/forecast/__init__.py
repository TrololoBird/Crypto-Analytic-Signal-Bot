"""Scanner forecast bands — re-export from maps (Phase 2 consolidation)."""
from hunt_core.maps.forecast import (  # noqa: F401
    build_all_forecasts,
    build_dump_forecast,
    build_ignition_forecast,
    build_maps_forecast,
)

__all__ = [
    "build_all_forecasts",
    "build_dump_forecast",
    "build_ignition_forecast",
    "build_maps_forecast",
]
