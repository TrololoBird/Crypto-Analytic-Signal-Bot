from __future__ import annotations

from ..domain.config import BotSettings
from ..domain.schemas import PreparedSymbol, Signal
from .roadmap_base import RoadmapSetup
from ..setups.detectors.atr_expansion import detect_atr_expansion_prepared

class ATRExpansionSetup(RoadmapSetup):
    setup_id = "atr_expansion"
    family = "volatility"
    confirmation_profile = "breakout_acceptance"
    required_context = ("futures_flow",)
    DEFAULTS = {
        **RoadmapSetup.DEFAULTS,
        "atr_mean_window": 20,
        "min_atr_expansion_ratio": 1.75,
        "min_body_atr": 0.25,
        "signal_lookback_bars": 8,
    }

    def detect(self, prepared: PreparedSymbol, settings: BotSettings) -> Signal | None:
        return detect_atr_expansion_prepared(
            prepared,
            settings,
            self._params(prepared, settings),
            setup_id=self.setup_id,
            family=self.family,
        )


__all__ = ["ATRExpansionSetup"]
