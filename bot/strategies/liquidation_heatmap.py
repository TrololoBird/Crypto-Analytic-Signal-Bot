from __future__ import annotations

from ..domain.config import BotSettings
from ..domain.schemas import PreparedSymbol, Signal
from .roadmap_base import RoadmapSetup
from ..setups.detectors.liquidation_heatmap import detect_liquidation_heatmap


class LiquidationHeatmapSetup(RoadmapSetup):
    setup_id = "liquidation_heatmap"
    family = "liquidity"
    confirmation_profile = "countertrend_exhaustion"
    required_context = ("futures_flow",)
    DEFAULTS = {
        **RoadmapSetup.DEFAULTS,
        "min_liquidation_score": 0.30,
        "min_oi_drop_pct": 0.03,
        "min_proxy_volume_ratio": 1.20,
        "min_proxy_wick_atr": 0.25,
        "proxy_lookback_bars": 12,
        "min_close_position_long": 0.55,
        "max_close_position_short": 0.45,
        "min_volume_ratio": 0.90,
    }

    def detect(self, prepared: PreparedSymbol, settings: BotSettings) -> Signal | None:
        return detect_liquidation_heatmap(
            prepared,
            settings,
            self._params(prepared, settings),
            setup_id=self.setup_id,
            family=self.family,
        )


__all__ = ["LiquidationHeatmapSetup"]
