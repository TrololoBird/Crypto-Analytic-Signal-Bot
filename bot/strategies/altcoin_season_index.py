from __future__ import annotations

from ..domain.config import BotSettings
from ..domain.schemas import PreparedSymbol, Signal
from .roadmap_base import RoadmapSetup
from ..setups.detectors.altcoin_season_index import detect_altcoin_season_index


class AltcoinSeasonIndexSetup(RoadmapSetup):
    setup_id = "altcoin_season_index"
    family = "multi_asset"
    confirmation_profile = "trend_follow"
    required_context = ("futures_flow",)
    DEFAULTS = {
        **RoadmapSetup.DEFAULTS,
        "altseason_long_threshold": 55.0,
        "btc_dominance_threshold": 45.0,
        "min_volume_ratio": 0.80,
        "min_roc10_abs_pct": 0.10,
        "min_relative_vs_btc_pct": 0.15,
        "sl_buffer_atr": 1.20,
    }

    def detect(self, prepared: PreparedSymbol, settings: BotSettings) -> Signal | None:
        return detect_altcoin_season_index(
            prepared,
            settings,
            self._params(prepared, settings),
            setup_id=self.setup_id,
            family=self.family,
        )


__all__ = ["AltcoinSeasonIndexSetup"]
