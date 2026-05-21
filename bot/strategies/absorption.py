from __future__ import annotations

from ..domain.config import BotSettings
from ..domain.schemas import PreparedSymbol, Signal
from .roadmap_base import (
    RoadmapSetup,
    _build_atr_signal,
    _flow_delta_with_source,
    _last,
    _reject,
)
from .spec_patterns import build_spec_signal, detect_absorption

class AbsorptionSetup(RoadmapSetup):
    setup_id = "absorption"
    family = "orderflow"
    confirmation_profile = "countertrend_exhaustion"
    required_context = ("futures_flow",)
    DEFAULTS = {
        **RoadmapSetup.DEFAULTS,
        "min_abs_flow_delta": 0.05,
        "min_close_position_long": 0.55,
        "max_close_position_short": 0.45,
        "min_wick_atr": 0.12,
        "min_volume_ratio": 0.90,
    }

    def detect(self, prepared: PreparedSymbol, settings: BotSettings) -> Signal | None:
        params = self._params(prepared, settings)
        hit = detect_absorption(prepared.work_15m, timeframe="15m")
        if hit is None:
            _reject(prepared, self.setup_id, "pattern.absorption_not_confirmed")
            return None
        return build_spec_signal(
            prepared=prepared,
            settings=settings,
            setup_id=self.setup_id,
            family=self.family,
            hit=hit,
            defaults=self.DEFAULTS,
            params=params,
        )

        flow, flow_source = _flow_delta_with_source(prepared)
        if flow is None:
            _reject(prepared, self.setup_id, "orderflow_delta_missing")
            return None
        work = prepared.work_15m
        close_position = _last(work, "close_position", 0.5)
        atr = _last(work, "atr14")
        high = _last(work, "high")
        low = _last(work, "low")
        close = _last(work, "close")
        open_ = _last(work, "open")
        if min(atr, high, low, close, open_) <= 0.0:
            _reject(prepared, self.setup_id, "invalid_indicator_state", atr=atr)
            return None
        lower_wick_atr = (min(open_, close) - low) / atr
        upper_wick_atr = (high - max(open_, close)) / atr
        vol_ratio = _last(work, "volume_ratio20", 1.0)
        volume_penalty = vol_ratio < float(params["min_volume_ratio"])
        if (
            flow <= -float(params["min_abs_flow_delta"])
            and close_position >= float(params["min_close_position_long"])
            and lower_wick_atr >= float(params["min_wick_atr"])
        ):
            direction = "long"
        elif (
            flow >= float(params["min_abs_flow_delta"])
            and close_position <= float(params["max_close_position_short"])
            and upper_wick_atr >= float(params["min_wick_atr"])
        ):
            direction = "short"
        else:
            _reject(prepared, self.setup_id, "absorption_not_confirmed", flow_delta=flow)
            return None
        clarity = min(abs(flow) * 2.0, 1.0)
        if flow_source != "agg_trade":
            clarity *= 0.72 if flow_source == "ohlcv_delta_proxy" else 0.85
        if volume_penalty:
            clarity *= 0.90
        return _build_atr_signal(
            prepared=prepared,
            setup_id=self.setup_id,
            direction=direction,
            params=params,
            reasons=[
                f"absorption_{direction}",
                f"flow_delta={flow:.3f}",
                f"flow_source={flow_source}",
                f"close_position={close_position:.2f}",
                f"volume_ratio={vol_ratio:.2f}",
            ],
            family=self.family,
            structure_clarity=clarity,
        )


__all__ = ["AbsorptionSetup"]
