from __future__ import annotations

from ..domain.config import BotSettings
from ..domain.schemas import PreparedSymbol, Signal
from .roadmap_base import RoadmapSetup
from ..setups.detectors.depth_imbalance import detect_depth_imbalance


class DepthImbalanceSetup(RoadmapSetup):
    setup_id = "depth_imbalance"
    family = "orderbook"
    confirmation_profile = "breakout_acceptance"
    required_context = ("futures_flow",)
    DEFAULTS = {
        **RoadmapSetup.DEFAULTS,
        "min_depth_imbalance": 0.3334,
        "min_microprice_bias": 0.05,
        "min_close_position_long": 0.52,
        "max_close_position_short": 0.48,
        "min_volume_ratio": 0.80,
        "min_roc10_abs_pct": 0.00,
        "min_rr": 1.9,
    }

    def detect(self, prepared: PreparedSymbol, settings: BotSettings) -> Signal | None:
        return detect_depth_imbalance(
            prepared,
            settings,
            self._params(prepared, settings),
            setup_id=self.setup_id,
            family=self.family,
        )


__all__ = ["DepthImbalanceSetup"]
