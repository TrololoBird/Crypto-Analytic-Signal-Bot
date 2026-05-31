from __future__ import annotations

from ..domain.config import BotSettings
from ..domain.schemas import PreparedSymbol, Signal
from .roadmap_base import RoadmapSetup
from ..setups.detectors.oi_divergence import detect_oi_divergence


class OIDivergenceSetup(RoadmapSetup):
    setup_id = "oi_divergence"
    family = "sentiment"
    confirmation_profile = "countertrend_exhaustion"
    required_context = ("futures_flow",)
    DEFAULTS = {
        **RoadmapSetup.DEFAULTS,
        "min_abs_oi_change_pct": 0.005,
        "min_price_change_pct": 0.06,
    }

    def detect(self, prepared: PreparedSymbol, settings: BotSettings) -> Signal | None:
        return detect_oi_divergence(
            prepared,
            settings,
            self._params(prepared, settings),
            setup_id=self.setup_id,
            family=self.family,
        )


__all__ = ["OIDivergenceSetup"]
