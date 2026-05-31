from __future__ import annotations

from ..domain.config import BotSettings
from ..domain.schemas import PreparedSymbol, Signal
from .roadmap_base import RoadmapSetup
from ..setups.detectors.bb_squeeze import detect_bb_squeeze_prepared


class BBSqueezeSetup(RoadmapSetup):
    setup_id = "bb_squeeze"
    family = "volatility"
    confirmation_profile = "breakout_acceptance"
    required_context = ("futures_flow",)
    DEFAULTS = {
        **RoadmapSetup.DEFAULTS,
        "max_bb_width": 5.0,
        "min_volume_ratio": 0.90,
        "min_roc10_abs_pct": 0.10,
        "squeeze_release_lookback": 8.0,
        "squeeze_memory_bars": 20.0,
    }

    def detect(self, prepared: PreparedSymbol, settings: BotSettings):
        return detect_bb_squeeze_prepared(
            prepared,
            settings,
            self._params(prepared, settings),
            setup_id=self.setup_id,
            family=self.family,
        )


__all__ = ["BBSqueezeSetup"]
