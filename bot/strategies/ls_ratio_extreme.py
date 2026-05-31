from __future__ import annotations

from ..domain.config import BotSettings
from ..domain.schemas import PreparedSymbol, Signal
from .roadmap_base import RoadmapSetup
from ..setups.detectors.ls_ratio_extreme import detect_ls_ratio_extreme


class LSRatioExtremeSetup(RoadmapSetup):
    setup_id = "ls_ratio_extreme"
    family = "sentiment"
    confirmation_profile = "countertrend_exhaustion"
    required_context = ("futures_flow",)
    DEFAULTS = {
        **RoadmapSetup.DEFAULTS,
        "long_account_threshold": 0.65,
        "short_account_threshold": 0.35,
        "soft_long_account_threshold": 0.58,
        "soft_short_account_threshold": 0.42,
        "min_close_position_long": 0.58,
        "max_close_position_short": 0.42,
        "min_volume_ratio": 0.90,
        "max_adverse_depth_imbalance": 0.10,
        "max_adverse_microprice_bias": 0.10,
        "sl_buffer_atr": 1.10,
        "min_rr": 1.9,
        "min_oi_change_pct": 0.5,
        "oi_missing_penalty": 0.92,
    }

    def detect(self, prepared: PreparedSymbol, settings: BotSettings) -> Signal | None:
        return detect_ls_ratio_extreme(
            prepared,
            settings,
            self._params(prepared, settings),
            setup_id=self.setup_id,
            family=self.family,
        )


__all__ = ["LSRatioExtremeSetup"]
