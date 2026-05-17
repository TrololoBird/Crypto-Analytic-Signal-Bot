"""Price velocity breakout setup.

# WINDSURF_REVIEW: unified + vectorized + 1H context + graded
"""

from __future__ import annotations

import math

from ..domain.config import BotSettings
from ..domain.schemas import PreparedSymbol, Signal
from ..setup_base import BaseSetup
from ..setups import _build_signal, _compute_dynamic_score, _reject
from ..setups.utils import get_dynamic_params


def _as_float(value: object, default: float = 0.0) -> float:
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        numeric = float(value)
        return numeric if math.isfinite(numeric) else default
    return default


def _finite_or_none(value: object) -> float | None:
    if value is None:
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    return numeric if math.isfinite(numeric) else None


def _orderflow_against_direction(
    prepared: PreparedSymbol,
    direction: str,
    *,
    max_adverse_depth: float,
    max_adverse_micro: float,
) -> tuple[bool, dict[str, float]]:
    depth = _finite_or_none(prepared.depth_imbalance)
    micro = _finite_or_none(prepared.microprice_bias)
    details: dict[str, float] = {}
    if depth is not None:
        details["depth_imbalance"] = depth
    if micro is not None:
        details["microprice_bias"] = micro

    if direction == "long":
        adverse_depth = depth is not None and depth <= -max_adverse_depth
        adverse_micro = micro is not None and micro <= -max_adverse_micro
    else:
        adverse_depth = depth is not None and depth >= max_adverse_depth
        adverse_micro = micro is not None and micro >= max_adverse_micro
    return bool(adverse_depth or adverse_micro), details


class PriceVelocitySetup(BaseSetup):
    setup_id = "price_velocity"
    family = "breakout"
    confirmation_profile = "breakout_acceptance"
    required_context = ("futures_flow",)

    def get_optimizable_params(self, settings: BotSettings | None = None) -> dict[str, float]:
        defaults = {
            "base_score": 0.53,
            "min_roc10_abs_pct": 0.75,
            "min_body_atr": 0.55,
            "min_volume_ratio": 1.35,
            "max_rsi_long": 82.0,
            "min_rsi_short": 18.0,
            "sl_buffer_atr": 0.55,
            "min_rr": 1.9,
            "min_adx_1h": 16.0,
            "max_adverse_depth_imbalance": 0.12,
            "max_adverse_microprice_bias": 0.12,
            "strict_1h_structure": 1.0,
        }
        if settings is not None:
            setups = getattr(getattr(settings, "filters", None), "setups", {})
            if isinstance(setups, dict) and self.setup_id in setups:
                return {**defaults, **setups.get(self.setup_id, {})}
        return defaults

    def detect(self, prepared: PreparedSymbol, settings: BotSettings) -> Signal | None:
        setup_id = self.setup_id
        work = prepared.work_15m
        if work.height < 30:
            _reject(prepared, setup_id, "insufficient_15m_bars")
            return None

        required = (
            "open",
            "high",
            "low",
            "close",
            "atr14",
            "roc10",
            "volume_ratio20",
            "close_position",
            "rsi14",
        )
        missing = [column for column in required if column not in work.columns]
        if missing:
            _reject(prepared, setup_id, "missing_columns", missing_fields=missing)
            return None

        params = self.get_optimizable_params(settings)
        dynamic_params = get_dynamic_params(prepared, setup_id)
        effective_params = {**params, **dynamic_params}

        open_ = _as_float(work.item(-1, "open"))
        high = _as_float(work.item(-1, "high"))
        low = _as_float(work.item(-1, "low"))
        close = _as_float(work.item(-1, "close"))
        atr = _as_float(work.item(-1, "atr14"))
        roc10 = _as_float(work.item(-1, "roc10"))
        vol_ratio = _as_float(work.item(-1, "volume_ratio20"), 1.0)
        close_position = _as_float(work.item(-1, "close_position"), 0.5)
        rsi = _as_float(work.item(-1, "rsi14"), 50.0)

        if min(open_, high, low, close, atr) <= 0.0 or math.isnan(atr):
            _reject(prepared, setup_id, "invalid_indicator_state", atr=atr)
            return None

        min_roc = float(effective_params["min_roc10_abs_pct"])
        body_atr = abs(close - open_) / atr
        if abs(roc10) < min_roc and body_atr < float(effective_params["min_body_atr"]):
            _reject(
                prepared,
                setup_id,
                "velocity_too_low",
                roc10=roc10,
                body_atr=body_atr,
            )
            return None
        if vol_ratio < float(effective_params["min_volume_ratio"]):
            _reject(prepared, setup_id, "volume_too_low", volume_ratio=vol_ratio)
            return None

        direction: str | None = None
        if roc10 > 0.0 and close > open_ and close_position >= 0.65:
            direction = "long"
        elif roc10 < 0.0 and close < open_ and close_position <= 0.35:
            direction = "short"

        if direction is None:
            _reject(prepared, setup_id, "direction_not_confirmed", rsi=rsi)
            return None

        adx_1h = (
            _as_float(prepared.work_1h.item(-1, "adx14"), 0.0)
            if not prepared.work_1h.is_empty() and "adx14" in prepared.work_1h.columns
            else 0.0
        )
        min_adx_1h = float(effective_params.get("min_adx_1h", 0.0))
        if adx_1h < min_adx_1h:
            _reject(
                prepared,
                setup_id,
                "adx_too_low",
                adx_1h=adx_1h,
                min_adx_1h=min_adx_1h,
            )
            return None

        orderflow_conflict, orderflow_details = _orderflow_against_direction(
            prepared,
            direction,
            max_adverse_depth=float(effective_params.get("max_adverse_depth_imbalance", 0.12)),
            max_adverse_micro=float(effective_params.get("max_adverse_microprice_bias", 0.12)),
        )
        if orderflow_conflict:
            _reject(
                prepared,
                setup_id,
                "orderflow_against_velocity",
                **orderflow_details,
            )
            return None

        if float(effective_params.get("strict_1h_structure", 1.0)) > 0.0:
            structure_1h = str(getattr(prepared, "structure_1h", "") or "")
            regime_1h = str(getattr(prepared, "regime_1h_confirmed", "") or "")
            if direction == "long" and (structure_1h == "downtrend" or regime_1h == "downtrend"):
                _reject(
                    prepared,
                    setup_id,
                    "structure_against_velocity",
                    structure_1h=structure_1h,
                    regime_1h_confirmed=regime_1h,
                )
                return None
            if direction == "short" and (structure_1h == "uptrend" or regime_1h == "uptrend"):
                _reject(
                    prepared,
                    setup_id,
                    "structure_against_velocity",
                    structure_1h=structure_1h,
                    regime_1h_confirmed=regime_1h,
                )
                return None

        bias_1h = getattr(prepared, "bias_1h", prepared.bias_4h)
        sl_buffer = float(effective_params["sl_buffer_atr"])
        min_rr = float(effective_params["min_rr"])
        if direction == "long":
            stop = min(low, open_) - atr * sl_buffer
            risk = close - stop
            tp1 = close + risk * min_rr
            tp2 = close + risk * max(2.0, min_rr + 0.35)
        else:
            stop = max(high, open_) + atr * sl_buffer
            risk = stop - close
            tp1 = close - risk * min_rr
            tp2 = close - risk * max(2.0, min_rr + 0.35)
        if risk <= 0.0:
            _reject(prepared, setup_id, "invalid_stop", stop=stop, close=close)
            return None

        base_score = float(effective_params["base_score"])
        score = _compute_dynamic_score(
            direction=direction,
            base_score=base_score,
            vol_ratio=vol_ratio,
            rsi=rsi,
            structure_clarity=min(abs(roc10) / 2.5, 1.0),
        )

        # Graded bias alignment
        if direction == "long" and bias_1h == "downtrend":
            score *= effective_params.get("bias_mismatch_penalty", 0.75)
        elif direction == "short" and bias_1h == "uptrend":
            score *= effective_params.get("bias_mismatch_penalty", 0.75)

        # RSI extremes graded penalty
        if direction == "long" and rsi > float(effective_params["max_rsi_long"]):
            score *= 0.85
        elif direction == "short" and rsi < float(effective_params["min_rsi_short"]):
            score *= 0.85

        reasons = [
            f"price_velocity_{direction}",
            f"roc10={roc10:.2f}",
            f"body_atr={body_atr:.2f}",
            f"vol_ratio={vol_ratio:.2f}",
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
