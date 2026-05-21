from __future__ import annotations

from ..domain.config import BotSettings
from ..domain.schemas import PreparedSymbol, Signal
from .roadmap_base import (
    RoadmapSetup,
    _build_atr_signal,
    _finite_or_none,
    _last,
    _reject,
)

class MultiTFTrendSetup(RoadmapSetup):
    setup_id = "multi_tf_trend"
    family = "continuation"
    confirmation_profile = "trend_follow"
    required_context = ("futures_flow",)
    DEFAULTS = {
        **RoadmapSetup.DEFAULTS,
        "min_adx_1h": 15.0,
        "min_volume_ratio": 0.90,
        "pullback_rsi_long_max": 58.0,
        "pullback_rsi_short_min": 42.0,
        "max_adverse_depth_imbalance": 1.00,
        "min_trend_votes": 2,
    }

    def detect(self, prepared: PreparedSymbol, settings: BotSettings) -> Signal | None:
        params = self._params(prepared, settings)
        htf = prepared.work_4h if prepared.work_4h is not None and prepared.work_4h.height >= 55 else prepared.work_1h
        ltf = prepared.work_15m
        if htf.height < 55 or ltf.height < 10:
            _reject(
                prepared,
                self.setup_id,
                "insufficient_history",
                htf_bars=htf.height,
                ltf_bars=ltf.height,
            )
            return None
        if "ema50" not in htf.columns:
            _reject(prepared, self.setup_id, "required_feature_missing", missing_fields=("ema50",))
            return None
        htf_ema = htf["ema50"]
        htf_slope = float(htf_ema[-1] - htf_ema[-5])
        ltf_delta = _last(ltf, "close") - _last(ltf.head(ltf.height - 5), "close")
        if htf_slope > 0.0 and ltf_delta >= 0.0:
            spec_direction = "long"
        elif htf_slope < 0.0 and ltf_delta <= 0.0:
            spec_direction = "short"
        else:
            _reject(
                prepared,
                self.setup_id,
                "pattern.multi_tf_not_aligned",
                htf_slope=htf_slope,
                ltf_delta=ltf_delta,
            )
            return None

        adx_1h = _last(prepared.work_1h, "adx14")
        vol_ratio = _last(prepared.work_15m, "volume_ratio20", 1.0)
        rsi_15m = _last(prepared.work_15m, "rsi14", 50.0)
        if adx_1h < float(params["min_adx_1h"]):
            _reject(prepared, self.setup_id, "adx_too_low", adx_1h=adx_1h)
            return None
        if vol_ratio < float(params["min_volume_ratio"]):
            _reject(prepared, self.setup_id, "volume_too_low", volume_ratio=vol_ratio)
            return None
        context_values = [
            prepared.bias_4h,
            prepared.bias_1h,
            prepared.regime_4h_confirmed,
            prepared.regime_1h_confirmed,
        ]
        up_votes = sum(1 for value in context_values if value == "uptrend")
        down_votes = sum(1 for value in context_values if value == "downtrend")
        min_votes = int(params.get("min_trend_votes", 3))
        if up_votes >= min_votes and up_votes > down_votes:
            direction = "long"
        elif down_votes >= min_votes and down_votes > up_votes:
            direction = "short"
        else:
            _reject(
                prepared,
                self.setup_id,
                "multi_tf_not_aligned",
                up_votes=up_votes,
                down_votes=down_votes,
                min_votes=min_votes,
            )
            return None
        if direction != spec_direction:
            _reject(
                prepared,
                self.setup_id,
                "pattern.multi_tf_not_aligned",
                htf_slope=htf_slope,
                ltf_delta=ltf_delta,
                context_direction=direction,
                spec_direction=spec_direction,
            )
            return None
        if direction == "long" and rsi_15m > float(params["pullback_rsi_long_max"]):
            _reject(
                prepared,
                self.setup_id,
                "pullback_quality_missing",
                direction=direction,
                rsi_15m=rsi_15m,
                max_rsi=float(params["pullback_rsi_long_max"]),
            )
            return None
        if direction == "short" and rsi_15m < float(params["pullback_rsi_short_min"]):
            _reject(
                prepared,
                self.setup_id,
                "pullback_quality_missing",
                direction=direction,
                rsi_15m=rsi_15m,
                min_rsi=float(params["pullback_rsi_short_min"]),
            )
            return None
        depth = _finite_or_none(prepared.depth_imbalance)
        max_adverse_depth = float(params.get("max_adverse_depth_imbalance", 1.00))
        if direction == "long" and depth is not None and depth <= -max_adverse_depth:
            _reject(
                prepared,
                self.setup_id,
                "orderflow_against_trend_pullback",
                depth_imbalance=depth,
            )
            return None
        if direction == "short" and depth is not None and depth >= max_adverse_depth:
            _reject(
                prepared,
                self.setup_id,
                "orderflow_against_trend_pullback",
                depth_imbalance=depth,
            )
            return None
        return _build_atr_signal(
            prepared=prepared,
            setup_id=self.setup_id,
            direction=direction,
            params=params,
            reasons=[
                f"multi_tf_pullback_{direction}",
                f"adx_1h={adx_1h:.1f}",
                f"htf_ema50_slope={htf_slope:.6f}",
                f"rsi15={rsi_15m:.1f}",
                f"votes_up={up_votes} votes_down={down_votes}",
            ],
            family=self.family,
            structure_clarity=0.85,
        )


__all__ = ["MultiTFTrendSetup"]
