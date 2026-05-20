from __future__ import annotations

from ..domain.config import BotSettings
from ..domain.schemas import PreparedSymbol, Signal
from .roadmap_base import (
    RoadmapSetup,
    _build_atr_signal,
    _confirmed_context_conflict,
    _finite_or_none,
    _last,
    _reject,
)

class LiquidationHeatmapSetup(RoadmapSetup):
    setup_id = "liquidation_heatmap"
    family = "liquidity"
    confirmation_profile = "countertrend_exhaustion"
    required_context = ("futures_flow",)
    DEFAULTS = {
        **RoadmapSetup.DEFAULTS,
        "min_liquidation_score": 0.30,
        "min_proxy_volume_ratio": 1.20,
        "min_proxy_wick_atr": 0.25,
        "proxy_lookback_bars": 12,
        "min_close_position_long": 0.55,
        "max_close_position_short": 0.45,
        "min_volume_ratio": 0.90,
    }

    def detect(self, prepared: PreparedSymbol, settings: BotSettings) -> Signal | None:
        params = self._params(prepared, settings)
        score = _finite_or_none(prepared.liquidation_score)
        source = str(getattr(prepared, "liquidation_score_source", None) or "missing")
        close_position = _last(prepared.work_15m, "close_position", 0.5)
        vol_ratio = _last(prepared.work_15m, "volume_ratio20", 1.0)
        volume_penalty = vol_ratio < float(params["min_volume_ratio"])
        if score is None or source != "force_order":
            _reject(
                prepared,
                self.setup_id,
                "liquidation_score_missing",
                liquidation_source=source,
                volume_ratio=vol_ratio,
            )
            return None

        threshold = float(params["min_liquidation_score"])
        if score >= threshold and close_position >= float(params["min_close_position_long"]):
            direction = "long"
        elif score <= -threshold and close_position <= float(params["max_close_position_short"]):
            direction = "short"
        else:
            _reject(
                prepared,
                self.setup_id,
                "liquidation_cluster_not_actionable",
                liquidation_score=score,
            )
            return None
        context_penalty = _confirmed_context_conflict(prepared, direction)
        clarity = min(abs(score), 1.0)
        if source != "force_order":
            clarity *= 0.75
        if volume_penalty:
            clarity *= 0.90
        if context_penalty:
            clarity *= 0.82
        return _build_atr_signal(
            prepared=prepared,
            setup_id=self.setup_id,
            direction=direction,
            params=params,
            reasons=[
                f"liquidation_heatmap_{direction}",
                f"source={source}",
                f"liq_score={score:.2f}",
                f"volume_ratio={vol_ratio:.2f}",
            ],
            family=self.family,
            structure_clarity=clarity,
        )


__all__ = ["LiquidationHeatmapSetup"]
