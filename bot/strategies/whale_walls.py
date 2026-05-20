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

class WhaleWallsSetup(RoadmapSetup):
    setup_id = "whale_walls"
    family = "orderbook"
    confirmation_profile = "breakout_acceptance"
    required_context = ("futures_flow",)
    DEFAULTS = {
        **RoadmapSetup.DEFAULTS,
        "min_depth_imbalance": 0.45,
        "min_microprice_bias": 0.20,
        "min_volume_ratio": 0.90,
        "min_close_position_long": 0.55,
        "max_close_position_short": 0.45,
        "max_spread_bps": 8.0,
        "min_roc10_abs_pct": 0.05,
        "min_rr": 1.9,
    }

    def detect(self, prepared: PreparedSymbol, settings: BotSettings) -> Signal | None:
        params = self._params(prepared, settings)
        depth = _finite_or_none(prepared.depth_imbalance)
        micro = _finite_or_none(prepared.microprice_bias)
        if depth is None or micro is None:
            _reject(
                prepared,
                self.setup_id,
                "orderbook_context_incomplete",
                depth_imbalance=depth,
                microprice_bias=micro,
            )
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
        spread = _finite_or_none(prepared.spread_bps)
        if spread is not None and spread > float(params["max_spread_bps"]):
            _reject(prepared, self.setup_id, "spread_too_wide", spread_bps=spread)
            return None
        depth_value = float(depth)
        micro_value = float(micro)
        work = prepared.work_15m
        vol_ratio = _last(work, "volume_ratio20", 1.0)
        volume_penalty = vol_ratio < float(params["min_volume_ratio"])
        close_position = _last(work, "close_position", 0.5)
        roc10 = _last(work, "roc10", _price_change_pct(work, 10))
        if abs(roc10) < float(params["min_roc10_abs_pct"]):
            _reject(prepared, self.setup_id, "price_acceptance_missing", roc10=roc10)
            return None
        long_votes = sum(
            (
                depth_value >= float(params["min_depth_imbalance"]),
                micro_value >= float(params["min_microprice_bias"]),
                close_position >= float(params["min_close_position_long"]),
                roc10 >= 0.0,
            )
        )
        short_votes = sum(
            (
                depth_value <= -float(params["min_depth_imbalance"]),
                micro_value <= -float(params["min_microprice_bias"]),
                close_position <= float(params["max_close_position_short"]),
                roc10 <= 0.0,
            )
        )
        if long_votes >= 3 and long_votes > short_votes:
            direction = "long"
        elif short_votes >= 3 and short_votes > long_votes:
            direction = "short"
        else:
            reason = (
                "wall_proxy_conflict" if depth_value * micro_value < 0.0 else "wall_proxy_too_weak"
            )
            _reject(
                prepared,
                self.setup_id,
                reason,
                depth_imbalance=depth_value,
                microprice_bias=micro_value,
                close_position=close_position,
            )
            return None
        context_penalty = _confirmed_context_conflict(prepared, direction)
        clarity = min(abs(depth_value), 1.0)
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
                f"orderbook_wall_proxy_{direction}",
                f"depth_imbalance={depth_value:.3f}",
                f"depth_source={_orderbook_source(prepared)}",
                f"micro={micro_value:.3f}",
                f"close_position={close_position:.2f}",
                f"volume_ratio={vol_ratio:.2f}",
                f"roc10={roc10:.2f}",
                f"votes={long_votes if direction == 'long' else short_votes}",
            ],
            family=self.family,
            structure_clarity=clarity,
        )


__all__ = ["WhaleWallsSetup"]
