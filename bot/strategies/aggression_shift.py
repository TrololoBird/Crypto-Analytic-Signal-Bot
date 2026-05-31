from __future__ import annotations

from ..domain.config import BotSettings
from ..domain.schemas import PreparedSymbol, Signal
from .roadmap_base import RoadmapSetup
from ..setups.detectors.aggression_shift import detect_aggression_shift_prepared

class AggressionShiftSetup(RoadmapSetup):
    setup_id = "aggression_shift"
    family = "orderflow"
    confirmation_profile = "breakout_acceptance"
    required_context = ("futures_flow",)
    DEFAULTS = {
        **RoadmapSetup.DEFAULTS,
        "min_shift": 0.05,
        "min_proxy_shift": 0.025,
        "shift_std_mult": 0.75,
        "delta_spike_mult": 2.0,
        "min_volume_ratio": 0.90,
        "signal_lookback_bars": 6,
        "max_entry_drift_atr": 0.75,
    }

    def detect(self, prepared: PreparedSymbol, settings: BotSettings) -> Signal | None:
        return detect_aggression_shift_prepared(
            prepared,
            settings,
            self._params(prepared, settings),
            setup_id=self.setup_id,
            family=self.family,
        )


__all__ = ["AggressionShiftSetup"]
