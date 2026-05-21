from __future__ import annotations

import math

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
        "min_proxy_shift": 0.025,
        "shift_std_mult": 0.75,
        "min_volume_ratio": 0.90,
    }

    @staticmethod
    def _adaptive_shift_threshold(prepared: PreparedSymbol, params: dict[str, float]) -> float:
        configured = float(params.get("min_shift", 0.05))
        proxy_floor = float(params.get("min_proxy_shift", 0.025))
        std_mult = float(params.get("shift_std_mult", 0.75))
        work = prepared.work_15m
        if work.height < 8 or "delta_ratio" not in work.columns:
            return max(0.015, min(configured, proxy_floor))
        values = [
            float(value) - 0.5
            for value in work["delta_ratio"].tail(24).to_list()
            if value is not None and math.isfinite(float(value))
        ]
        if len(values) < 6:
            return max(0.015, min(configured, proxy_floor))
        mean = sum(values) / len(values)
        variance = sum((value - mean) ** 2 for value in values) / max(1, len(values) - 1)
        adaptive = max(proxy_floor, math.sqrt(max(0.0, variance)) * std_mult)
        return max(0.015, min(configured, adaptive))

    def detect(self, prepared: PreparedSymbol, settings: BotSettings) -> Signal | None:
        params = self._params(prepared, settings)
        hit = detect_aggression_shift(prepared.work_15m, timeframe="15m")
        if hit is not None:
            return build_spec_signal(
                prepared=prepared,
                settings=settings,
                setup_id=self.setup_id,
                family=self.family,
                hit=hit,
                defaults=self.DEFAULTS,
                params=params,
            )

        # FIX 2026-05-21: strict spec miss must fall through to the configured
        # public orderflow proxy path instead of making the fallback unreachable.
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
        threshold = self._adaptive_shift_threshold(prepared, params)
        if abs(shift) < threshold and "delta_ratio" in prepared.work_15m.columns:
            current_delta = _last(prepared.work_15m, "delta_ratio", 0.5)
            shift = current_delta - 0.5
        if shift >= threshold:
            direction = "long"
        elif shift <= -threshold:
            direction = "short"
        else:
            _reject(
                prepared,
                self.setup_id,
                "aggression_shift_too_small",
                shift=shift,
                adaptive_threshold=threshold,
            )
            return None
        return _build_atr_signal(
            prepared=prepared,
            setup_id=self.setup_id,
            direction=direction,
            params=params,
            reasons=[
                f"aggression_shift_{direction}",
                f"shift={shift:.3f}",
                f"threshold={threshold:.3f}",
                f"flow_source={shift_source}",
            ],
            family=self.family,
            structure_clarity=min(abs(shift) * 3.0, 1.0)
            * (0.72 if shift_source != "agg_trade" else 1.0)
            * (0.90 if volume_penalty else 1.0),
        )


__all__ = ["AggressionShiftSetup"]
