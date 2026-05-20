from __future__ import annotations

from ..domain.config import BotSettings
from ..domain.schemas import PreparedSymbol, Signal
from .roadmap_base import (
    RoadmapSetup,
    _build_atr_signal,
    _confirmed_context_conflict,
    _finite_or_none,
    _has_l2_depth,
    _last,
    _orderbook_source,
    _price_change_pct,
    _reject,
)

class DepthImbalanceSetup(RoadmapSetup):
    setup_id = "depth_imbalance"
    family = "orderbook"
    confirmation_profile = "breakout_acceptance"
    required_context = ("futures_flow",)
    DEFAULTS = {
        **RoadmapSetup.DEFAULTS,
        "min_depth_imbalance": 0.20,
        "min_microprice_bias": 0.05,
        "min_close_position_long": 0.52,
        "max_close_position_short": 0.48,
        "min_volume_ratio": 0.80,
        "min_roc10_abs_pct": 0.00,
        "min_rr": 1.9,
    }

    def detect(self, prepared: PreparedSymbol, settings: BotSettings) -> Signal | None:
        params = self._params(prepared, settings)
        depth = _finite_or_none(prepared.depth_imbalance)
        if depth is None:
            _reject(prepared, self.setup_id, "depth_imbalance_missing")
            return None
        micro = _finite_or_none(prepared.microprice_bias)
        if micro is None:
            _reject(prepared, self.setup_id, "microprice_bias_missing")
            return None
        if not _has_l2_depth(prepared):
            _reject(
                prepared,
                self.setup_id,
                "depth_l2_missing",
                depth_source=_orderbook_source(prepared),
                depth_imbalance=depth,
                microprice_bias=micro,
            )
            return None
        close_position = _last(prepared.work_15m, "close_position", 0.5)
        threshold = float(params["min_depth_imbalance"])
        micro_threshold = float(params["min_microprice_bias"])
        vol_ratio = _last(prepared.work_15m, "volume_ratio20", 1.0)
        volume_penalty = vol_ratio < float(params["min_volume_ratio"])
        roc10 = _last(prepared.work_15m, "roc10", _price_change_pct(prepared.work_15m, 10))
        if abs(roc10) < float(params["min_roc10_abs_pct"]):
            _reject(prepared, self.setup_id, "price_acceptance_missing", roc10=roc10)
            return None
        long_votes = sum(
            (
                depth >= threshold,
                micro >= micro_threshold,
                close_position >= float(params["min_close_position_long"]),
                roc10 >= 0.0,
            )
        )
        short_votes = sum(
            (
                depth <= -threshold,
                micro <= -micro_threshold,
                close_position <= float(params["max_close_position_short"]),
                roc10 <= 0.0,
            )
        )
        if long_votes >= 2 and long_votes > short_votes:
            direction = "long"
        elif short_votes >= 2 and short_votes > long_votes:
            direction = "short"
        else:
            _reject(
                prepared,
                self.setup_id,
                "depth_not_actionable",
                depth_imbalance=depth,
                microprice_bias=micro,
                close_position=close_position,
            )
            return None
        context_penalty = _confirmed_context_conflict(prepared, direction)
        clarity = min(abs(depth), 1.0)
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
                f"depth_imbalance_{direction}",
                f"depth={depth:.3f}",
                f"depth_source={_orderbook_source(prepared)}",
                f"micro={micro:.3f}",
                f"volume_ratio={vol_ratio:.2f}",
                f"roc10={roc10:.2f}",
                f"votes={long_votes if direction == 'long' else short_votes}",
            ],
            family=self.family,
            structure_clarity=clarity,
        )


__all__ = ["DepthImbalanceSetup"]
