from __future__ import annotations

import math

from ..domain.config import BotSettings
from ..domain.schemas import PreparedSymbol, Signal
from .roadmap_base import (
    RoadmapSetup,
    _build_atr_signal,
    _finite_or_none,
    _last,
    _prev,
    _reject,
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
        "delta_spike_mult": 2.0,
        "min_volume_ratio": 0.90,
    }

    @staticmethod
    def _delta_shift_gate(
        prepared: PreparedSymbol,
        params: dict[str, float],
    ) -> tuple[float, float, str] | None:
        work = prepared.work_15m
        configured = float(params.get("min_shift", 0.05))
        proxy_floor = float(params.get("min_proxy_shift", 0.025))
        spike_mult = float(params.get("delta_spike_mult", 2.0))
        if work.height >= 22 and "delta_ratio" in work.columns:
            values = [
                float(value) - 0.5
                for value in work["delta_ratio"].tail(21).to_list()
                if value is not None and math.isfinite(float(value))
            ]
            if len(values) >= 6:
                current = values[-1]
                baseline = values[:-1][-20:]
                mean_abs = sum(abs(value) for value in baseline) / max(1, len(baseline))
                threshold = max(configured, proxy_floor, mean_abs * spike_mult)
                return current, threshold, "ohlcv_delta_proxy"

        explicit_shift = _finite_or_none(prepared.aggression_shift)
        if explicit_shift is None:
            return None
        threshold = max(configured, proxy_floor)
        return explicit_shift, threshold, str(getattr(prepared, "orderflow_source", None) or "agg_trade")

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
        gate = self._delta_shift_gate(prepared, params)
        if gate is None:
            _reject(prepared, self.setup_id, "aggression_shift_missing")
            return None
        shift, threshold, shift_source = gate
        vol_ratio = _last(prepared.work_15m, "volume_ratio20", 1.0)
        volume_penalty = vol_ratio < float(params["min_volume_ratio"])
        if abs(shift) < threshold:
            _reject(
                prepared,
                self.setup_id,
                "pattern.aggression_shift_too_small",
                shift=shift,
                threshold=threshold,
            )
            return None

        close = _last(prepared.work_15m, "close")
        prev_close = _prev(prepared.work_15m, "close")
        if min(close, prev_close) <= 0.0:
            _reject(prepared, self.setup_id, "price_context_missing")
            return None
        price_up = close > prev_close
        price_down = close < prev_close
        if price_up and shift < 0.0:
            direction = "short"
        elif price_down and shift > 0.0:
            direction = "long"
        else:
            _reject(
                prepared,
                self.setup_id,
                "pattern.no_direction_conflict",
                shift=shift,
                price_change=close - prev_close,
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
