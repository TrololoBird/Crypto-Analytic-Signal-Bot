from __future__ import annotations

from ..domain.config import BotSettings
from ..domain.schemas import PreparedSymbol, Signal
from ..setups.base import BaseSetup
from ..setups.detectors.session_killzone import (
    _active_killzone_name,
    _latest_bar_time_utc,
    detect_session_killzone,
)
from ..setups.utils import get_dynamic_params


class SessionKillzoneSetup(BaseSetup):
    setup_id = "session_killzone"
    family = "breakout"
    confirmation_profile = "breakout_acceptance"
    required_context = ("futures_flow",)

    def get_optimizable_params(self, settings: BotSettings | None = None) -> dict[str, float]:
        defaults = {
            "base_score": 0.55,
            "min_volume_ratio": 1.0,
            "min_adx_1h": 14.0,
            "sl_buffer_atr": 0.75,
            "bias_mismatch_penalty": 0.75,
            "min_rr": 1.9,
            "breakout_lookback_bars": 20,
            "breakout_atr_mult": 0.05,
            "min_close_position_long": 0.58,
            "max_close_position_short": 0.42,
            "max_adverse_depth_imbalance": 0.10,
            "max_adverse_microprice_bias": 0.10,
            "strict_1h_structure": 0.0,
            "orderflow_conflict_penalty": 0.88,
            "structure_conflict_penalty": 0.82,
            "asia_start_hour_utc": 0,
            "asia_end_hour_utc": 6,
            "london_start_hour_utc": 7,
            "london_end_hour_utc": 9,
            "ny_start_hour_utc": 12,
            "ny_end_hour_utc": 14,
            "overlap_start_hour_utc": 12,
            "overlap_end_hour_utc": 14,
        }
        if settings is not None:
            filters = getattr(settings, "filters", None)
            if filters:
                setups_config = getattr(filters, "setups", {})
                if isinstance(setups_config, dict) and self.setup_id in setups_config:
                    return {**defaults, **setups_config.get(self.setup_id, {})}
        return defaults

    def active_session_name(
        self,
        prepared: PreparedSymbol,
        settings: BotSettings | None = None,
    ) -> str | None:
        params = self.get_optimizable_params(settings)
        dynamic_params = get_dynamic_params(prepared, self.setup_id)
        now_utc = _latest_bar_time_utc(prepared)
        return _active_killzone_name(
            now_utc.hour,
            {**params, **dynamic_params},
        )

    def is_active_now(
        self,
        prepared: PreparedSymbol,
        settings: BotSettings | None = None,
    ) -> bool:
        return self.active_session_name(prepared, settings) is not None

    def detect(self, prepared: PreparedSymbol, settings: BotSettings) -> Signal | None:
        return detect_session_killzone(
            prepared,
            settings,
            {**self.get_optimizable_params(settings), **get_dynamic_params(prepared, self.setup_id)},
            setup_id=self.setup_id,
            family=self.family,
        )


__all__ = ["SessionKillzoneSetup"]
