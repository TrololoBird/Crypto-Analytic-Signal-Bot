from __future__ import annotations

from ..domain.config import BotSettings
from ..domain.schemas import PreparedSymbol, Signal
from .roadmap_base import RoadmapSetup
from ..setups.detectors.rsi_divergence_bottom import detect_rsi_divergence_bottom

class RSIDivergenceBottomSetup(RoadmapSetup):
    setup_id = "rsi_divergence_bottom"
    family = "reversal"
    confirmation_profile = "countertrend_exhaustion"
    required_context = ("futures_flow",)
    DEFAULTS = {
        **RoadmapSetup.DEFAULTS,
        "divergence_window": 18,
        "min_rsi_delta": 1.0,
        "min_price_delta_pct": 0.03,
        "near_retest_pct": 0.18,
        "min_recovery_rsi_delta": 3.0,
        "max_long_rsi": 52.0,
        "min_short_rsi": 48.0,
        "min_reversal_close_position_long": 0.50,
        "max_reversal_close_position_short": 0.50,
        "adaptive_retest_penalty": 0.90,
        "rsi_recovery_penalty": 0.86,
    }

    def detect(self, prepared: PreparedSymbol, settings: BotSettings) -> Signal | None:
        return detect_rsi_divergence_bottom(
            prepared,
            settings,
            self._params(prepared, settings),
            setup_id=self.setup_id,
            family=self.family,
        )


__all__ = ["RSIDivergenceBottomSetup"]
