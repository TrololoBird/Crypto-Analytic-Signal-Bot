from __future__ import annotations

from ..domain.config import BotSettings
from ..domain.schemas import PreparedSymbol, Signal
from .roadmap_base import RoadmapSetup
from ..setups.detectors.multi_tf_trend import detect_multi_tf_trend


class MultiTFTrendSetup(RoadmapSetup):
    setup_id = "multi_tf_trend"
    family = "continuation"
    confirmation_profile = "trend_follow"
    required_context = ("futures_flow",)
    DEFAULTS = {
        **RoadmapSetup.DEFAULTS,
        "min_adx_1h": 15.0,
        "min_volume_ratio": 0.90,
        "pullback_rsi_long_max": 62.0,
        "pullback_rsi_short_min": 38.0,
        "max_adverse_depth_imbalance": 1.00,
        "min_trend_votes": 2,
    }

    def detect(self, prepared: PreparedSymbol, settings: BotSettings) -> Signal | None:
        return detect_multi_tf_trend(
            prepared,
            settings,
            self._params(prepared, settings),
            setup_id=self.setup_id,
            family=self.family,
        )


__all__ = ["MultiTFTrendSetup"]
