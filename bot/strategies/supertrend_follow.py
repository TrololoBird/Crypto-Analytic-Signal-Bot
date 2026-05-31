from __future__ import annotations

from ..domain.config import BotSettings
from ..domain.schemas import PreparedSymbol, Signal
from ..setups.base import BaseSetup
from ..setups.detectors.supertrend_follow import detect_supertrend_follow


class SuperTrendFollowSetup(BaseSetup):
    setup_id = "supertrend_follow"
    family = "continuation"
    confirmation_profile = "trend_follow"
    required_context = ("futures_flow",)

    def get_optimizable_params(self, settings: BotSettings | None = None) -> dict[str, float]:
        defaults = {
            "base_score": 0.56,
            "min_adx_1h": 12.0,
            "min_volume_ratio": 1.0,
            "volume_penalty": 0.92,
            "pullback_atr_threshold": 0.65,
            "ema_pullback_atr": 0.65,
            "ema_acceptance_atr": 0.35,
            "ema_reclaim_lookback_bars": 6,
            "max_ema_extension_atr": 1.25,
            "sl_buffer_atr": 0.65,
            "min_rr": 1.9,
        }
        if settings is not None:
            setups = getattr(getattr(settings, "filters", None), "setups", {})
            if isinstance(setups, dict) and self.setup_id in setups:
                return {**defaults, **setups.get(self.setup_id, {})}
        return defaults

    def detect(self, prepared: PreparedSymbol, settings: BotSettings) -> Signal | None:
        from ..setups.utils import get_dynamic_params

        return detect_supertrend_follow(
            prepared,
            settings,
            {**self.get_optimizable_params(settings), **get_dynamic_params(prepared, self.setup_id)},
            setup_id=self.setup_id,
            family=self.family,
        )


__all__ = ["SuperTrendFollowSetup"]
