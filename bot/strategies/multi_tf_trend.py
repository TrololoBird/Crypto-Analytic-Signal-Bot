from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from ._roadmap import (
    _build_atr_signal,
    _finite_or_none,
    _last,
    _prev,
    _reject,
)
from .roadmap_base import RoadmapSetup

if TYPE_CHECKING:
    from ..domain.config import BotSettings
    from ..domain.schemas import PreparedSymbol, Signal

__all__ = ["detect_multi_tf_trend"]


def detect_multi_tf_trend(
    prepared: PreparedSymbol,
    _settings: BotSettings,
    effective_params: dict[str, float],
    *,
    setup_id: str,
    family: str,
) -> Signal | None:
    params = effective_params
    htf = (
        prepared.work_4h
        if prepared.work_4h is not None and prepared.work_4h.height >= 55
        else prepared.work_1h
    )
    ltf = prepared.work_15m
    if ltf.height < 10:
        _reject(prepared, setup_id, "insufficient_ltf_bars", bars=ltf.height)
        return None
    if htf.height < 55:
        _reject(
            prepared,
            setup_id,
            "insufficient_history",
            htf_bars=htf.height,
            ltf_bars=ltf.height,
        )
        return None
    if "ema50" not in htf.columns or "atr14" not in htf.columns:
        missing = [c for c in ("ema50", "atr14") if c not in htf.columns]
        _reject(prepared, setup_id, "required_feature_missing", missing_fields=tuple(missing))
        return None
    htf_ema = htf["ema50"]
    htf_atr = float(htf["atr14"][-1]) if htf["atr14"][-1] is not None else 0.0
    htf_slope = float(htf_ema[-1] - htf_ema[-5])
    htf_slope_atr = (htf_slope / htf_atr) if htf_atr > 0.0 else 0.0
    min_slope_atr = float(params.get("min_slope_atr", 0.03))
    if ltf.height < 6:
        _reject(prepared, setup_id, "insufficient_bars")
        return None
    ltf_delta = _prev(ltf, "close") - float(ltf.item(-6, "close"))
    ltf_atr = _prev(ltf, "atr14", 0.0)
    ltf_noise = (
        max(ltf_atr * 0.08, abs(htf_slope) * 0.05) if ltf_atr > 0.0 else abs(htf_slope) * 0.05
    )
    if htf_slope_atr > min_slope_atr and ltf_delta >= -ltf_noise:
        spec_direction = "long"
    elif htf_slope_atr < -min_slope_atr and ltf_delta <= ltf_noise:
        spec_direction = "short"
    else:
        _reject(
            prepared,
            setup_id,
            "pattern.multi_tf_not_aligned",
            htf_slope=htf_slope,
            ltf_delta=ltf_delta,
        )
        return None

    adx_1h = _last(prepared.work_1h, "adx14")
    vol_ratio = _prev(prepared.work_15m, "volume_ratio20", 1.0)
    rsi_15m = _prev(prepared.work_15m, "rsi14", 50.0)
    if adx_1h < float(params["min_adx_1h"]):
        _reject(prepared, setup_id, "adx_too_low", adx_1h=adx_1h)
        return None
    if vol_ratio < float(params["min_volume_ratio"]):
        _reject(prepared, setup_id, "volume_too_low", volume_ratio=vol_ratio)
        return None

    ichimoku_cloud_penalty = 1.0
    if "senkou_a" in htf.columns and "senkou_b" in htf.columns:
        sa = _last(htf, "senkou_a")
        sb = _last(htf, "senkou_b")
        if sa is not None and sb is not None:
            cloud_top = max(sa, sb)
            cloud_bottom = min(sa, sb)
            close_htf = _last(htf, "close")
            if close_htf is not None:
                if close_htf < cloud_top and close_htf > cloud_bottom:
                    ichimoku_cloud_penalty = 0.80
                elif (
                    (close_htf < cloud_bottom and spec_direction == "long")
                    or (close_htf > cloud_top and spec_direction == "short")
                ):
                    _reject(prepared, setup_id, "ichimoku_cloud_opposes", sa=sa, sb=sb)
                    return None

    context_values = [
        prepared.bias_4h,
        prepared.bias_1h,
        prepared.regime_4h_confirmed,
        prepared.regime_1h_confirmed,
    ]
    up_votes = sum(1 for value in context_values if value == "uptrend")
    down_votes = sum(1 for value in context_values if value == "downtrend")
    min_votes = int(params.get("min_trend_votes", 1))
    if up_votes >= min_votes and up_votes > down_votes:
        direction = "long"
    elif down_votes >= min_votes and down_votes > up_votes:
        direction = "short"
    else:
        direction = spec_direction
    if direction != spec_direction:
        _reject(
            prepared,
            setup_id,
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
            setup_id,
            "pullback_quality_missing",
            direction=direction,
            rsi_15m=rsi_15m,
            max_rsi=float(params["pullback_rsi_long_max"]),
        )
        return None
    if direction == "short" and rsi_15m < float(params["pullback_rsi_short_min"]):
        _reject(
            prepared,
            setup_id,
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
            setup_id,
            "orderflow_against_trend_pullback",
            depth_imbalance=depth,
        )
        return None
    if direction == "short" and depth is not None and depth >= max_adverse_depth:
        _reject(
            prepared,
            setup_id,
            "orderflow_against_trend_pullback",
            depth_imbalance=depth,
        )
        return None
    entry_anchor = float(htf_ema[-1]) if htf_ema[-1] > 0 else None
    clarity = 0.85 * ichimoku_cloud_penalty
    reasons = [
        f"multi_tf_pullback_{direction}",
        f"adx_1h={adx_1h:.1f}",
        f"htf_ema50_slope_atr={htf_slope_atr:.4f}",
        f"rsi15={rsi_15m:.1f}",
        f"votes_up={up_votes} votes_down={down_votes}",
    ]
    if ichimoku_cloud_penalty < 1.0:
        reasons.append("ichimoku_cloud_inside")
    return _build_atr_signal(
        prepared=prepared,
        setup_id=setup_id,
        direction=direction,
        params=params,
        reasons=reasons,
        family=family,
        entry_anchor=entry_anchor,
        structure_clarity=clarity,
        confirmed_bar=True,
    )


class MultiTFTrendSetup(RoadmapSetup):
    setup_id = "multi_tf_trend"
    family = "continuation"
    confirmation_profile = "trend_follow"
    required_context = ("futures_flow",)
    DEFAULTS: ClassVar[dict[str, float]] = {
        **RoadmapSetup.DEFAULTS,
        "min_adx_1h": 15.0,
        "min_volume_ratio": 0.90,
        "pullback_rsi_long_max": 62.0,
        "pullback_rsi_short_min": 38.0,
        "max_adverse_depth_imbalance": 1.00,
        "min_trend_votes": 2,
        "min_slope_atr": 0.03,
    }

    def detect(self, prepared: PreparedSymbol, settings: BotSettings) -> Signal | None:
        return detect_multi_tf_trend(
            prepared,
            settings,
            self._params(prepared, settings),
            setup_id=self.setup_id,
            family=self.family,
        )


__all__ = ["MultiTFTrendSetup"]
