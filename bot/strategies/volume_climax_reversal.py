"""Volume climax reversal setup.

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
        return float(value)
    return default


class VolumeClimaxReversalSetup(BaseSetup):
    setup_id = "volume_climax_reversal"
    family = "reversal"
    confirmation_profile = "countertrend_exhaustion"
    required_context = ("futures_flow",)

    def get_optimizable_params(self, settings: BotSettings | None = None) -> dict[str, float]:
        defaults = {
            "base_score": 0.52,
            "min_volume_ratio": 1.8,
            "adaptive_min_volume_ratio": 1.30,
            "min_wick_atr": 0.45,
            "strong_wick_multiplier": 1.35,
            "max_rsi_long": 42.0,
            "min_rsi_short": 58.0,
            "sl_buffer_atr": 0.45,
            "min_rr": 1.9,
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
            "volume_ratio20",
            "close_position",
            "rsi14",
            "prev_donchian_low20",
            "prev_donchian_high20",
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
        vol_ratio = _as_float(work.item(-1, "volume_ratio20"), 1.0)
        close_position = _as_float(work.item(-1, "close_position"), 0.5)
        rsi = _as_float(work.item(-1, "rsi14"), 50.0)
        prev_low = _as_float(work.item(-1, "prev_donchian_low20"))
        prev_high = _as_float(work.item(-1, "prev_donchian_high20"))

        if min(open_, high, low, close, atr, prev_low, prev_high) <= 0.0 or math.isnan(atr):
            _reject(prepared, setup_id, "invalid_indicator_state", atr=atr)
            return None

        lower_wick_atr = (min(open_, close) - low) / atr
        upper_wick_atr = (high - max(open_, close)) / atr
        min_wick_atr = float(effective_params["min_wick_atr"])
        configured_min_volume = float(effective_params["min_volume_ratio"])
        adaptive_min_volume = float(effective_params.get("adaptive_min_volume_ratio", 1.30))
        strong_wick_multiplier = float(effective_params.get("strong_wick_multiplier", 1.35))
        direction: str | None = None
        clarity = 0.0
        if (
            low < prev_low
            and close > prev_low
            and lower_wick_atr >= min_wick_atr
            and close_position >= 0.55
        ):
            direction = "long"
            clarity = min(lower_wick_atr / 2.0, 1.0)
        elif (
            high > prev_high
            and close < prev_high
            and upper_wick_atr >= min_wick_atr
            and close_position <= 0.45
        ):
            direction = "short"
            clarity = min(upper_wick_atr / 2.0, 1.0)

        if direction is None:
            _reject(
                prepared,
                setup_id,
                "no_volume_climax_reclaim",
                lower_wick_atr=lower_wick_atr,
                upper_wick_atr=upper_wick_atr,
                rsi=rsi,
            )
            return None

        signal_wick_atr = lower_wick_atr if direction == "long" else upper_wick_atr
        strong_wick = signal_wick_atr >= (min_wick_atr * strong_wick_multiplier)
        required_volume = configured_min_volume
        if strong_wick:
            required_volume = min(configured_min_volume, adaptive_min_volume)
        if vol_ratio < required_volume:
            _reject(
                prepared,
                setup_id,
                "volume_climax_missing",
                volume_ratio=vol_ratio,
                required_volume_ratio=required_volume,
                adaptive_volume=strong_wick,
            )
            return None

        bias_1h = getattr(prepared, "bias_1h", prepared.bias_4h)
        sl_buffer = float(effective_params["sl_buffer_atr"])
        min_rr = float(effective_params["min_rr"])
        if direction == "long":
            stop = low - atr * sl_buffer
            risk = close - stop
            tp1 = close + risk * min_rr
            tp2 = close + risk * max(2.0, min_rr + 0.35)
        else:
            stop = high + atr * sl_buffer
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
            structure_clarity=clarity,
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
            f"volume_climax_reversal_{direction}",
            f"vol_ratio={vol_ratio:.2f}",
            f"lower_wick_atr={lower_wick_atr:.2f}",
            f"upper_wick_atr={upper_wick_atr:.2f}",
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
