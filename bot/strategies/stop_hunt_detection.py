from __future__ import annotations

from ..domain.config import BotSettings
from ..domain.schemas import PreparedSymbol, Signal
from .roadmap_base import RoadmapSetup
from ..setups.detectors.stop_hunt_detection import detect_stop_hunt_prepared

class StopHuntDetectionSetup(RoadmapSetup):
    setup_id = "stop_hunt_detection"
    family = "liquidity"
    confirmation_profile = "countertrend_exhaustion"
    required_context = ("futures_flow",)
    DEFAULTS = {
        **RoadmapSetup.DEFAULTS,
        "sweep_tolerance_pct": 0.0010,
        "min_volume_ratio": 0.80,
        "min_close_position_long": 0.48,
        "max_close_position_short": 0.52,
        "signal_lookback_bars": 24,
        "near_level_atr": 0.35,
        "min_wick_atr": 0.35,
        "max_entry_drift_atr": 1.25,
        "max_signal_lag_bars": 6,
        "weak_reclaim_penalty": 0.84,
        "sl_buffer_atr": 1.20,
        "min_recovery_delta_long": 0.49,
        "max_recovery_delta_short": 0.51,
        "max_adverse_depth_imbalance": 0.08,
        "max_adverse_microprice_bias": 0.08,
        "orderflow_conflict_penalty": 0.86,
    }

    def detect(self, prepared: PreparedSymbol, settings: BotSettings) -> Signal | None:
        return detect_stop_hunt_prepared(
            prepared,
            settings,
            self._params(prepared, settings),
            setup_id=self.setup_id,
            family=self.family,
        )


__all__ = ["StopHuntDetectionSetup"]
