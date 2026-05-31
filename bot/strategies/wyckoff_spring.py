from __future__ import annotations

from ..domain.config import BotSettings
from ..domain.schemas import PreparedSymbol, Signal
from .roadmap_base import RoadmapSetup
from ..setups.detectors.wyckoff_spring import detect_wyckoff_spring_prepared

class WyckoffSpringSetup(RoadmapSetup):
    setup_id = "wyckoff_spring"
    family = "reversal"
    confirmation_profile = "countertrend_exhaustion"
    required_context = ("futures_flow",)
    DEFAULTS = {
        **RoadmapSetup.DEFAULTS,
        "sweep_tolerance_pct": 0.0010,
        "min_volume_ratio": 1.05,
        "min_close_position_long": 0.48,
        "max_close_position_short": 0.52,
        "signal_lookback_bars": 18,
        "near_range_atr": 0.50,
        "min_wick_atr": 0.20,
        "max_signal_lag_bars": 6,
        "spring_volume_dryup_ratio": 1.10,
        "recovery_volume_ratio": 0.95,
        "volume_penalty": 0.90,
    }

    def detect(self, prepared: PreparedSymbol, settings: BotSettings) -> Signal | None:
        return detect_wyckoff_spring_prepared(
            prepared,
            settings,
            self._params(prepared, settings),
            setup_id=self.setup_id,
            family=self.family,
        )


__all__ = ["WyckoffSpringSetup"]
