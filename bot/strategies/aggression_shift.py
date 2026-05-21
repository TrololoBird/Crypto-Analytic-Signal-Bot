from __future__ import annotations

from ..domain.config import BotSettings
from ..domain.schemas import PreparedSymbol, Signal
from .roadmap_base import (
    RoadmapSetup,
    _build_atr_signal,
    _finite_or_none,
    _last,
    _reject,
    _series_mean_tail,
)
from .spec_patterns import build_spec_signal, detect_aggression_shift

class AggressionShiftSetup(RoadmapSetup):
    setup_id = "aggression_shift"
    family = "orderflow"
    confirmation_profile = "breakout_acceptance"
    required_context = ("futures_flow",)
    DEFAULTS = {
        **RoadmapSetup.DEFAULTS,
        "min_shift": 0.05,
        "min_volume_ratio": 0.90,
    }

    def detect(self, prepared: PreparedSymbol, settings: BotSettings) -> Signal | None:
        params = self._params(prepared, settings)
        hit = detect_aggression_shift(prepared.work_15m, timeframe="15m")
        if hit is None:
            _reject(prepared, self.setup_id, "pattern.aggression_shift_too_small")
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

        explicit_shift = _finite_or_none(prepared.aggression_shift)
        if explicit_shift is not None:
            shift = explicit_shift
            shift_source = str(getattr(prepared, "orderflow_source", None) or "agg_trade")
        elif prepared.work_15m.height >= 6 and "delta_ratio" in prepared.work_15m.columns:
            current_delta = _last(prepared.work_15m, "delta_ratio", 0.5)
            shift = current_delta - _series_mean_tail(
                prepared.work_15m.head(prepared.work_15m.height - 1),
                "delta_ratio",
                5,
            )
            shift_source = "ohlcv_delta_proxy"
        else:
            _reject(prepared, self.setup_id, "aggression_shift_missing")
            return None
        vol_ratio = _last(prepared.work_15m, "volume_ratio20", 1.0)
        volume_penalty = vol_ratio < float(params["min_volume_ratio"])
        if abs(shift) < float(params["min_shift"]) and "delta_ratio" in prepared.work_15m.columns:
            current_delta = _last(prepared.work_15m, "delta_ratio", 0.5)
            shift = current_delta - 0.5
        if shift >= float(params["min_shift"]):
            direction = "long"
        elif shift <= -float(params["min_shift"]):
            direction = "short"
        else:
            _reject(prepared, self.setup_id, "aggression_shift_too_small", shift=shift)
            return None
        return _build_atr_signal(
            prepared=prepared,
            setup_id=self.setup_id,
            direction=direction,
            params=params,
            reasons=[
                f"aggression_shift_{direction}",
                f"shift={shift:.3f}",
                f"flow_source={shift_source}",
            ],
            family=self.family,
            structure_clarity=min(abs(shift) * 3.0, 1.0)
            * (0.72 if shift_source != "agg_trade" else 1.0)
            * (0.90 if volume_penalty else 1.0),
        )


__all__ = ["AggressionShiftSetup"]
