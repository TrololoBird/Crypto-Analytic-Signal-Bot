"""VWAP reclaim trend-continuation setup.

# WINDSURF_REVIEW: unified + vectorized + 1H context + graded
"""

from __future__ import annotations

import math

from ..domain.config import BotSettings
from ..features import _swing_points
from ..domain.schemas import PreparedSymbol, Signal
from ..setup_base import BaseSetup
from ..setups import _build_signal, _compute_dynamic_score, _reject
from ..setups.utils import build_structural_targets, get_dynamic_params


def _as_float(value: object, default: float = 0.0) -> float:
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    return default


class VWAPTrendSetup(BaseSetup):
    setup_id = "vwap_trend"
    family = "continuation"
    confirmation_profile = "trend_follow"
    required_context = ("futures_flow",)

    def get_optimizable_params(self, settings: BotSettings | None = None) -> dict[str, float]:
        defaults = {
            "base_score": 0.55,
            "min_adx_1h": 15.0,
            "min_volume_ratio": 1.05,
            "vwap_reclaim_tolerance_pct": 0.0008,
            "min_rr": 1.5,
            "sl_buffer_atr": 0.7,
        }
        if settings is not None:
            setups = getattr(getattr(settings, "filters", None), "setups", {})
            if isinstance(setups, dict) and self.setup_id in setups:
                return {**defaults, **setups.get(self.setup_id, {})}
        return defaults

    def detect(self, prepared: PreparedSymbol, settings: BotSettings) -> Signal | None:
        setup_id = self.setup_id
        work_15m = prepared.work_15m
        work_1h = prepared.work_1h
        if work_15m.height < 30 or work_1h.height < 30:
            _reject(prepared, setup_id, "insufficient_bars")
            return None

        required_columns = (
            "close",
            "ema20",
            "vwap",
            "atr14",
            "volume_ratio20",
            "rsi14",
        )
        missing = [column for column in required_columns if column not in work_15m.columns]
        if missing:
            _reject(prepared, setup_id, "missing_columns", missing_fields=missing)
            return None

        params = self.get_optimizable_params(settings)
        dynamic_params = get_dynamic_params(prepared, setup_id)
        effective_params = {**params, **dynamic_params}

        close = _as_float(work_15m.item(-1, "close"))
        prev_close = _as_float(work_15m.item(-2, "close"))
        vwap = _as_float(work_15m.item(-1, "vwap"))
        prev_vwap = _as_float(work_15m.item(-2, "vwap"))
        ema20 = _as_float(work_15m.item(-1, "ema20"))
        atr = _as_float(work_15m.item(-1, "atr14"))
        vol_ratio = _as_float(work_15m.item(-1, "volume_ratio20"), 1.0)
        rsi = _as_float(work_15m.item(-1, "rsi14"), 50.0)
        adx_1h = _as_float(work_1h.item(-1, "adx14"))

        if min(close, prev_close, vwap, prev_vwap, ema20, atr) <= 0.0 or math.isnan(atr):
            _reject(
                prepared,
                setup_id,
                "invalid_indicator_state",
                close=close,
                vwap=vwap,
                ema20=ema20,
                atr=atr,
            )
            return None

        if adx_1h > 0.0 and adx_1h < float(effective_params["min_adx_1h"]):
            _reject(prepared, setup_id, "adx_too_low", adx_1h=adx_1h)
            return None

        if vol_ratio < float(effective_params["min_volume_ratio"]):
            _reject(
                prepared,
                setup_id,
                "volume_too_low",
                volume_ratio=vol_ratio,
            )
            return None

        tolerance = float(effective_params["vwap_reclaim_tolerance_pct"])
        bias_1h = getattr(prepared, "bias_1h", prepared.bias_4h)
        direction: str | None = None
        if (
            prev_close <= prev_vwap * (1.0 + tolerance)
            and close > vwap * (1.0 + tolerance)
            and close > ema20
        ):
            direction = "long"
        elif (
            prev_close >= prev_vwap * (1.0 - tolerance)
            and close < vwap * (1.0 - tolerance)
            and close < ema20
        ):
            direction = "short"

        if direction is None:
            _reject(prepared, setup_id, "no_vwap_reclaim", close=close, vwap=vwap)
            return None

        sh_mask, sl_mask = _swing_points(work_1h, n=3, include_unconfirmed_tail=True)
        stop_basis = min(vwap, ema20) if direction == "long" else max(vwap, ema20)
        stop, tp1, tp2 = build_structural_targets(
            direction=direction,
            price_anchor=close,
            stop_basis=stop_basis,
            atr=atr,
            work_1h=work_1h,
            work_4h=prepared.work_4h,
            min_rr=float(params["min_rr"]),
            sl_buffer_atr=float(params["sl_buffer_atr"]),
            sh_mask=sh_mask,
            sl_mask=sl_mask,
        )
        risk = abs(close - stop)
        if risk <= 0.0:
            _reject(prepared, setup_id, "invalid_stop", stop=stop, close=close)
            return None
        min_rr = float(params["min_rr"])
        if tp1 is None or abs(tp1 - close) < risk * min_rr:
            tp1 = close + risk * min_rr if direction == "long" else close - risk * min_rr
        if tp2 is None:
            tp2 = tp1

        base_score = float(effective_params["base_score"])
        score = _compute_dynamic_score(
            direction=direction,
            base_score=base_score,
            vol_ratio=vol_ratio,
            rsi=rsi,
            structure_clarity=0.45,
        )

        # Graded bias alignment
        if direction == "long" and bias_1h == "downtrend":
            score *= effective_params.get("bias_mismatch_penalty", 0.75)
        elif direction == "short" and bias_1h == "uptrend":
            score *= effective_params.get("bias_mismatch_penalty", 0.75)

        reasons = [
            f"vwap_reclaim_{direction}",
            f"bias_1h={bias_1h}",
            f"vwap={vwap:.4f}",
            f"vol_ratio={vol_ratio:.2f}",
            f"adx_1h={adx_1h:.1f}",
        ]
        return _build_signal(
            prepared=prepared,
            setup_id=setup_id,
            direction=direction,
            score=score,
            timeframe="15m",
            reasons=reasons,
            strategy_family=self.family,
            stop=stop,
            tp1=tp1,
            tp2=tp2,
            price_anchor=close,
            atr=atr,
        )
