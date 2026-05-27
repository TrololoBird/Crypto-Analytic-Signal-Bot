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
        "long_account_threshold": 0.65,
        "short_account_threshold": 0.35,
        "soft_long_account_threshold": 0.58,
        "soft_short_account_threshold": 0.42,
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
            _reject(prepared, self.setup_id, "data.ls_ratio_missing")
            return None
        ls_ratio = ratio
        long_account = ls_ratio / (1.0 + ls_ratio) if ls_ratio > 0.0 else 0.5
        # soft thresholds are fallback - must be looser than hard thresholds
        long_threshold = float(params.get("soft_long_account_threshold", 0.58))
        short_threshold = float(params.get("soft_short_account_threshold", 0.42))
        soft_extreme = False
        if long_account >= float(params["long_account_threshold"]):
            direction = "short"
        elif long_account <= float(params["short_account_threshold"]):
            direction = "long"
        elif long_account >= long_threshold:
            direction = "short"
            soft_extreme = True
        elif long_account <= short_threshold:
            direction = "long"
            soft_extreme = True
        else:
            _reject(
                prepared,
                self.setup_id,
                "indicator.ls_ratio_not_extreme",
                ls_ratio=ls_ratio,
                long_account=long_account,
                long_threshold=long_threshold,
                short_threshold=short_threshold,
            )
            return None
        work = prepared.work_15m
        close_position = _last(work, "close_position", 0.5)
        volume_ratio = _last(work, "volume_ratio20", 1.0)
        if volume_ratio < float(params["min_volume_ratio"]):
            volume_penalty = True
        else:
            volume_penalty = False
        if direction == "long":
            close_ok = close_position <= float(params["max_close_position_short"])
        else:
            close_ok = close_position >= float(params["min_close_position_long"])
        if not close_ok:
            if soft_extreme:
                price_confirmation_penalty = True
            else:
                _reject(
                    prepared,
                    self.setup_id,
                    "ls_ratio_price_confirmation_missing",
                    ls_ratio=ls_ratio,
                    close_position=close_position,
                    direction=direction,
                )
                return None
        else:
            price_confirmation_penalty = False

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
            f"long_account={long_account:.2f}",
            f"close_position={close_position:.2f}",
            f"volume_ratio={volume_ratio:.2f}",
        ]
        if soft_extreme:
            reasons.append("soft_crowd_extreme_penalty")
        if price_confirmation_penalty:
            reasons.append("price_confirmation_penalty")
        if volume_penalty:
            reasons.append("volume_confirmation_penalty")
        if orderbook_penalty:
            reasons.append("orderbook_against_penalty")
        if context_penalty:
            reasons.append("context_conflict_penalty")
        score_multiplier = 1.0
        if soft_extreme:
            score_multiplier *= 0.86
        if price_confirmation_penalty:
            score_multiplier *= 0.84
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
            structure_clarity=min(abs(long_account - 0.5) * 3.0, 1.0) * score_multiplier,
        )


__all__ = ["LSRatioExtremeSetup"]
