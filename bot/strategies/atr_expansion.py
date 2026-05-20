from __future__ import annotations

from ..domain.config import BotSettings
from ..domain.schemas import PreparedSymbol, Signal
from .roadmap_base import (
    RoadmapSetup,
    _as_float,
    _build_atr_signal,
    _last,
    _missing_columns,
    _reject,
    _series_mean_tail,
)

class ATRExpansionSetup(RoadmapSetup):
    setup_id = "atr_expansion"
    family = "volatility"
    confirmation_profile = "breakout_acceptance"
    required_context = ("futures_flow",)
    DEFAULTS = {
        **RoadmapSetup.DEFAULTS,
        "atr_mean_window": 20,
        "min_atr_expansion_ratio": 1.08,
        "min_body_atr": 0.25,
        "signal_lookback_bars": 6,
    }

    def detect(self, prepared: PreparedSymbol, settings: BotSettings) -> Signal | None:
        params = self._params(prepared, settings)
        work = prepared.work_15m
        missing = _missing_columns(work, ("open", "close", "atr14"))
        if missing:
            _reject(prepared, self.setup_id, "missing_columns", missing_fields=missing)
            return None
        lookback = max(1, int(params.get("signal_lookback_bars", 6)))
        recent = work.tail(min(lookback, work.height))
        atr = _last(work, "atr14")
        mean_atr = _series_mean_tail(work, "atr14", int(params["atr_mean_window"]))
        if atr <= 0.0 or mean_atr <= 0.0:
            _reject(prepared, self.setup_id, "atr_invalid", atr=atr, mean_atr=mean_atr)
            return None
        best_idx = recent.height - 1
        ratio = 0.0
        body_atr = 0.0
        for local_idx in range(recent.height):
            bar_atr = _as_float(recent.item(local_idx, "atr14"))
            if bar_atr <= 0.0:
                continue
            candidate_ratio = bar_atr / mean_atr
            candidate_body = abs(
                _as_float(recent.item(local_idx, "close"))
                - _as_float(recent.item(local_idx, "open"))
            ) / bar_atr
            if candidate_ratio + candidate_body > ratio + body_atr:
                ratio = candidate_ratio
                body_atr = candidate_body
                best_idx = local_idx
        if ratio < float(params["min_atr_expansion_ratio"]) or body_atr < float(
            params["min_body_atr"]
        ):
            _reject(prepared, self.setup_id, "atr_expansion_too_low", atr_ratio=ratio)
            return None
        signal_open = _as_float(recent.item(best_idx, "open"))
        signal_close = _as_float(recent.item(best_idx, "close"))
        direction = "long" if signal_close >= signal_open else "short"
        signal_lag = recent.height - 1 - best_idx
        return _build_atr_signal(
            prepared=prepared,
            setup_id=self.setup_id,
            direction=direction,
            params=params,
            reasons=[
                f"atr_expansion_{direction}",
                f"atr_ratio={ratio:.2f}",
                f"body_atr={body_atr:.2f}",
                f"signal_lag={signal_lag}",
            ],
            family=self.family,
            structure_clarity=min((ratio - 1.0) / 1.0, 1.0),
        )


__all__ = ["ATRExpansionSetup"]
