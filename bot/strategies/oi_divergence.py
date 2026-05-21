from __future__ import annotations

from ..domain.config import BotSettings
from ..domain.schemas import PreparedSymbol, Signal
from .roadmap_base import (
    RoadmapSetup,
    _build_atr_signal,
    _finite_or_none,
    _price_change_pct,
    _reject,
)

class OIDivergenceSetup(RoadmapSetup):
    setup_id = "oi_divergence"
    family = "sentiment"
    confirmation_profile = "countertrend_exhaustion"
    required_context = ("futures_flow",)
    DEFAULTS = {
        **RoadmapSetup.DEFAULTS,
        "min_abs_oi_change_pct": 0.01,
        "min_price_change_pct": 0.10,
    }

    def detect(self, prepared: PreparedSymbol, settings: BotSettings) -> Signal | None:
        params = self._params(prepared, settings)
        oi_change = _finite_or_none(prepared.oi_change_pct)
        if oi_change is None:
            _reject(prepared, self.setup_id, "asset_fit.oi_missing")
            return None
        price_change = _price_change_pct(prepared.work_15m, 8)
        if abs(oi_change) < float(params["min_abs_oi_change_pct"]) or abs(price_change) < float(
            params["min_price_change_pct"]
        ):
            _reject(
                prepared,
                self.setup_id,
                "indicator.oi_price_divergence_too_small",
                oi_change_pct=oi_change,
            )
            return None
        if oi_change > 0.0:
            direction = "long" if price_change > 0.0 else "short"
            oi_context = "oi_confirms_price"
        elif price_change > 0.0:
            direction = "short"
            oi_context = "price_up_oi_contracting"
        else:
            direction = "long"
            oi_context = "price_down_oi_contracting"
        return _build_atr_signal(
            prepared=prepared,
            setup_id=self.setup_id,
            direction=direction,
            params=params,
            reasons=[
                f"oi_divergence_{direction}",
                oi_context,
                f"oi_change={oi_change:.2f}",
                f"price_change={price_change:.2f}",
            ],
            family=self.family,
            structure_clarity=min(abs(oi_change) / 0.05, 1.0),
        )


__all__ = ["OIDivergenceSetup"]
