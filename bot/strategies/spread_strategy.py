from __future__ import annotations

from ..domain.config import BotSettings
from ..domain.schemas import PreparedSymbol, Signal
from .roadmap_base import RoadmapSetup
from ..setups.detectors.spread_strategy import detect_spread_strategy


class SpreadStrategySetup(RoadmapSetup):
    setup_id = "spread_strategy"
    family = "orderbook"
    confirmation_profile = "breakout_acceptance"
    required_context = ("futures_flow",)
    DEFAULTS = {
        **RoadmapSetup.DEFAULTS,
        "max_spread_bps": 8.0,
        "min_volume_ratio": 0.90,
        "min_roc10_abs_pct": 0.15,
        "min_depth_imbalance": 0.10,
        "min_microprice_bias": 0.05,
        "min_close_position_long": 0.55,
        "max_close_position_short": 0.45,
        "min_rr": 1.9,
    }

    def detect(self, prepared: PreparedSymbol, settings: BotSettings) -> Signal | None:
        return detect_spread_strategy(
            prepared,
            settings,
            self._params(prepared, settings),
            setup_id=self.setup_id,
            family=self.family,
        )


__all__ = ["SpreadStrategySetup"]
