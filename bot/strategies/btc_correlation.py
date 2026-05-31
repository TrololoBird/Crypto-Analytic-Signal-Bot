from __future__ import annotations

from ..domain.config import BotSettings
from ..domain.schemas import PreparedSymbol, Signal
from .roadmap_base import RoadmapSetup
from ..setups.detectors.btc_correlation import detect_btc_correlation


class BTCCorrelationSetup(RoadmapSetup):
    setup_id = "btc_correlation"
    family = "multi_asset"
    confirmation_profile = "trend_follow"
    required_context = ("futures_flow",)
    DEFAULTS = {
        **RoadmapSetup.DEFAULTS,
        "min_roc10_abs_pct": 0.10,
        "min_volume_ratio": 0.70,
        "sl_buffer_atr": 1.00,
    }

    def detect(self, prepared: PreparedSymbol, settings: BotSettings) -> Signal | None:
        return detect_btc_correlation(
            prepared,
            settings,
            self._params(prepared, settings),
            setup_id=self.setup_id,
            family=self.family,
        )


__all__ = ["BTCCorrelationSetup"]
