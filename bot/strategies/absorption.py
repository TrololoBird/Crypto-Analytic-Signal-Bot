from __future__ import annotations

from ..domain.config import BotSettings
from ..domain.schemas import PreparedSymbol, Signal
from .roadmap_base import RoadmapSetup
from ..setups.detectors.absorption import detect_absorption_prepared

class AbsorptionSetup(RoadmapSetup):
    setup_id = "absorption"
    family = "orderflow"
    confirmation_profile = "countertrend_exhaustion"
    required_context = ("futures_flow",)
    DEFAULTS = {
        **RoadmapSetup.DEFAULTS,
        "min_abs_flow_delta": 0.05,
        "min_close_position_long": 0.55,
        "max_close_position_short": 0.45,
        "min_wick_atr": 0.12,
        "min_volume_ratio": 0.90,
    }

    def detect(self, prepared: PreparedSymbol, settings: BotSettings) -> Signal | None:
        return detect_absorption_prepared(
            prepared,
            settings,
            self._params(prepared, settings),
            setup_id=self.setup_id,
            family=self.family,
        )


__all__ = ["AbsorptionSetup"]
