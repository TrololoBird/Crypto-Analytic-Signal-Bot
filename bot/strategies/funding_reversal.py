from __future__ import annotations

from ..domain.config import BotSettings
from ..domain.schemas import PreparedSymbol, Signal
from ..setups.base import BaseSetup
from ..setups.detectors.funding_reversal import detect_funding_reversal


class FundingReversalSetup(BaseSetup):
    setup_id = "funding_reversal"
    family = "reversal"
    confirmation_profile = "countertrend_exhaustion"
    required_context = ("futures_flow",)
    requires_funding = True

    def get_optimizable_params(self, settings: BotSettings | None = None) -> dict[str, float]:
        defaults = {
            "base_score": 0.52,
            "funding_threshold": 0.0010,
            "funding_soft_threshold": 0.00010,
            "funding_trend_bars": 3.0,
            "funding_recent_extreme_lookback_hours": 48.0,
            "historical_funding_score_penalty": 0.92,
            "relative_funding_score_penalty": 0.82,
            "min_delta_threshold": 0.02,
            "confirmation_lookback_bars": 4,
            "min_confirmation_score": 0.70,
            "min_volume_ratio": 0.85,
            "sl_buffer_atr": 0.6,
            "bias_mismatch_penalty": 0.75,
            "min_rr": 1.9,
            "min_oi_change_pct": 0.5,
            "oi_unconfirmed_penalty": 0.90,
        }
        if settings is not None:
            filters = getattr(settings, "filters", None)
            if filters:
                setups_config = getattr(filters, "setups", {})
                if isinstance(setups_config, dict) and self.setup_id in setups_config:
                    return {**defaults, **setups_config.get(self.setup_id, {})}
        return defaults

    def detect(self, prepared: PreparedSymbol, settings: BotSettings) -> Signal | None:
        from ..setups.utils import get_dynamic_params

        return detect_funding_reversal(
            prepared,
            settings,
            {**self.get_optimizable_params(settings), **get_dynamic_params(prepared, self.setup_id)},
            setup_id=self.setup_id,
            family=self.family,
        )


__all__ = ["FundingReversalSetup"]
