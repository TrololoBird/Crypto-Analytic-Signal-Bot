from __future__ import annotations

from ..domain.config import BotSettings
from ..domain.schemas import PreparedSymbol, Signal
from .roadmap_base import (
    RoadmapSetup,
    _build_atr_signal,
    _confirmed_context_conflict,
    _finite_or_none,
    _first_finite,
    _last,
    _reject,
)

class LSRatioExtremeSetup(RoadmapSetup):
    setup_id = "ls_ratio_extreme"
    family = "sentiment"
    confirmation_profile = "countertrend_exhaustion"
    required_context = ("futures_flow",)
    DEFAULTS = {
        **RoadmapSetup.DEFAULTS,
        "long_crowd_threshold": 1.75,
        "short_crowd_threshold": 0.65,
        "min_close_position_long": 0.58,
        "max_close_position_short": 0.42,
        "min_volume_ratio": 0.90,
        "max_adverse_depth_imbalance": 0.10,
        "max_adverse_microprice_bias": 0.10,
        "sl_buffer_atr": 0.85,
        "min_rr": 1.9,
    }

    def detect(self, prepared: PreparedSymbol, settings: BotSettings) -> Signal | None:
        params = self._params(prepared, settings)
        ratio = _first_finite(
            prepared.top_account_ls_ratio,
            prepared.ls_ratio,
            prepared.global_ls_ratio,
        )
        if ratio is None:
            _reject(prepared, self.setup_id, "ls_ratio_missing")
            return None
        ls_ratio = ratio
        if ls_ratio >= float(params["long_crowd_threshold"]):
            direction = "short"
        elif ls_ratio <= float(params["short_crowd_threshold"]):
            direction = "long"
        else:
            _reject(prepared, self.setup_id, "ls_ratio_not_extreme", ls_ratio=ls_ratio)
            return None
        work = prepared.work_15m
        close_position = _last(work, "close_position", 0.5)
        volume_ratio = _last(work, "volume_ratio20", 1.0)
        if volume_ratio < float(params["min_volume_ratio"]):
            volume_penalty = True
        else:
            volume_penalty = False
        if direction == "long":
            close_ok = close_position >= float(params["min_close_position_long"])
        else:
            close_ok = close_position <= float(params["max_close_position_short"])
        if not close_ok:
            _reject(
                prepared,
                self.setup_id,
                "ls_ratio_price_confirmation_missing",
                ls_ratio=ls_ratio,
                close_position=close_position,
                direction=direction,
            )
            return None

        depth = _finite_or_none(prepared.depth_imbalance)
        micro = _finite_or_none(prepared.microprice_bias)
        max_depth = float(params["max_adverse_depth_imbalance"])
        max_micro = float(params["max_adverse_microprice_bias"])
        if direction == "long":
            adverse_depth = depth is not None and depth < -max_depth
            adverse_micro = micro is not None and micro < -max_micro
        else:
            adverse_depth = depth is not None and depth > max_depth
            adverse_micro = micro is not None and micro > max_micro
        if adverse_depth or adverse_micro:
            orderbook_penalty = True
        else:
            orderbook_penalty = False
        context_penalty = False
        if _confirmed_context_conflict(prepared, direction):
            context_penalty = True
        reasons = [
            f"ls_ratio_extreme_{direction}",
            f"ls_ratio={ls_ratio:.2f}",
            f"close_position={close_position:.2f}",
            f"volume_ratio={volume_ratio:.2f}",
        ]
        if volume_penalty:
            reasons.append("volume_confirmation_penalty")
        if orderbook_penalty:
            reasons.append("orderbook_against_penalty")
        if context_penalty:
            reasons.append("context_conflict_penalty")
        score_multiplier = 1.0
        if volume_penalty:
            score_multiplier *= 0.90
        if orderbook_penalty:
            score_multiplier *= 0.86
        if context_penalty:
            score_multiplier *= 0.82
        return _build_atr_signal(
            prepared=prepared,
            setup_id=self.setup_id,
            direction=direction,
            params=params,
            reasons=reasons,
            family=self.family,
            structure_clarity=min(abs(ls_ratio - 1.0), 1.0) * score_multiplier,
        )


__all__ = ["LSRatioExtremeSetup"]
