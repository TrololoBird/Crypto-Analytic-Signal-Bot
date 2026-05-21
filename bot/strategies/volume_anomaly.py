"""Volume anomaly momentum setup.

# WINDSURF_REVIEW: unified + vectorized + 1H context + graded
"""

from __future__ import annotations

import math

from ..domain.config import BotSettings
from ..domain.schemas import PreparedSymbol, Signal
from ..setup_base import BaseSetup
from ..setups import _build_signal, _compute_dynamic_score, _reject
from ..setups.utils import get_dynamic_params
from .spec_patterns import build_spec_signal, detect_volume_anomaly


def _as_float(value: object, default: float = 0.0) -> float:
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    return default


class VolumeAnomalySetup(BaseSetup):
    setup_id = "volume_anomaly"
    family = "breakout"
    confirmation_profile = "breakout_acceptance"
    required_context = ("futures_flow",)

    def get_optimizable_params(self, settings: BotSettings | None = None) -> dict[str, float]:
        defaults = {
            "base_score": 0.52,
            "min_volume_ratio": 1.6,
            "min_body_atr": 0.50,
            "min_close_position_long": 0.65,
            "max_close_position_short": 0.35,
            "signal_lookback_bars": 3,
            "max_rsi_long": 78.0,
            "min_rsi_short": 22.0,
            "sl_buffer_atr": 0.6,
            "min_rr": 1.9,
        }
        if settings is not None:
            setups = getattr(getattr(settings, "filters", None), "setups", {})
            if isinstance(setups, dict) and self.setup_id in setups:
                return {**defaults, **setups.get(self.setup_id, {})}
        return defaults

    def detect(self, prepared: PreparedSymbol, settings: BotSettings) -> Signal | None:
        setup_id = self.setup_id
        params = self.get_optimizable_params(settings)
        dynamic_params = get_dynamic_params(prepared, setup_id)
        effective_params = {**params, **dynamic_params}
        hit = detect_volume_anomaly(prepared.work_15m, timeframe="15m")
        if hit is not None:
            return build_spec_signal(
                prepared=prepared,
                settings=settings,
                setup_id=setup_id,
                family=self.family,
                hit=hit,
                defaults=params,
                params=effective_params,
            )

        # FIX 2026-05-21: spec demands a decisive latest candle; keep the
        # configured recent-bar volume anomaly fallback live on a miss.
        work = prepared.work_15m
        if work.height < 30:
            _reject(prepared, setup_id, "insufficient_15m_bars")
            return None

        required_columns = (
            "open",
            "high",
            "low",
            "close",
            "atr14",
            "volume_ratio20",
            "close_position",
            "rsi14",
        )
        missing = [column for column in required_columns if column not in work.columns]
        if missing:
            _reject(prepared, setup_id, "missing_columns", missing_fields=missing)
            return None

        params = self.get_optimizable_params(settings)
        dynamic_params = get_dynamic_params(prepared, setup_id)
        effective_params = {**params, **dynamic_params}

        close = _as_float(work.item(-1, "close"))
        atr = _as_float(work.item(-1, "atr14"))
        rsi = _as_float(work.item(-1, "rsi14"), 50.0)

        if min(close, atr) <= 0.0 or math.isnan(atr):
            _reject(prepared, setup_id, "invalid_indicator_state", atr=atr, close=close)
            return None

        direction: str | None = None
        signal_idx = work.height - 1
        signal_high = _as_float(work.item(-1, "high"))
        signal_low = _as_float(work.item(-1, "low"))
        signal_open = _as_float(work.item(-1, "open"))
        signal_close = close
        signal_close_position = _as_float(work.item(-1, "close_position"), 0.5)
        vol_ratio = _as_float(work.item(-1, "volume_ratio20"), 1.0)
        body_atr = 0.0
        signal_lookback = max(1, int(effective_params.get("signal_lookback_bars", 3)))
        recent = work.tail(min(signal_lookback, work.height))
        for local_idx in range(recent.height - 1, -1, -1):
            open_ = _as_float(recent.item(local_idx, "open"))
            high = _as_float(recent.item(local_idx, "high"))
            low = _as_float(recent.item(local_idx, "low"))
            bar_close = _as_float(recent.item(local_idx, "close"))
            bar_vol_ratio = _as_float(recent.item(local_idx, "volume_ratio20"), 1.0)
            close_position = _as_float(recent.item(local_idx, "close_position"), 0.5)
            candidate_body_atr = abs(bar_close - open_) / atr if atr > 0.0 else 0.0
            if bar_vol_ratio < float(effective_params["min_volume_ratio"]):
                continue
            if candidate_body_atr < float(effective_params["min_body_atr"]):
                continue
            if (
                bar_close > open_
                and close >= bar_close * 0.998
                and close_position >= float(effective_params["min_close_position_long"])
            ):
                direction = "long"
            elif (
                bar_close < open_
                and close <= bar_close * 1.002
                and close_position <= float(effective_params["max_close_position_short"])
            ):
                direction = "short"
            if direction is not None:
                signal_idx = work.height - recent.height + local_idx
                signal_high = high
                signal_low = low
                signal_open = open_
                signal_close = bar_close
                signal_close_position = close_position
                vol_ratio = max(vol_ratio, bar_vol_ratio)
                body_atr = candidate_body_atr
                break

        if direction is None:
            _reject(
                prepared,
                setup_id,
                "candle_close_not_decisive",
                close_position=signal_close_position,
                rsi=rsi,
            )
            return None

        bias_1h = getattr(prepared, "bias_1h", prepared.bias_4h)
        sl_buffer = float(effective_params["sl_buffer_atr"])
        min_rr = float(effective_params["min_rr"])
        price_anchor = (signal_high + signal_low) / 2.0
        if direction == "long":
            stop = min(signal_low, signal_open) - atr * sl_buffer
            risk = price_anchor - stop
            tp1 = price_anchor + risk * min_rr
            tp2 = price_anchor + risk * max(min_rr, 2.0)
        else:
            stop = max(signal_high, signal_open) + atr * sl_buffer
            risk = stop - price_anchor
            tp1 = price_anchor - risk * min_rr
            tp2 = price_anchor - risk * max(min_rr, 2.0)

        if risk <= 0.0:
            _reject(prepared, setup_id, "invalid_stop", stop=stop, close=close)
            return None

        base_score = float(effective_params["base_score"])
        score = _compute_dynamic_score(
            direction=direction,
            base_score=base_score,
            vol_ratio=vol_ratio,
            rsi=rsi,
            structure_clarity=min(body_atr / 2.0, 1.0),
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
            f"volume_anomaly_{direction}",
            f"vol_ratio={vol_ratio:.2f}",
            f"body_atr={body_atr:.2f}",
            f"close_position={signal_close_position:.2f}",
            f"signal_lag={work.height - 1 - signal_idx}",
            f"signal_close={signal_close:.4f}",
            f"limit_entry={price_anchor:.4f}",
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
            price_anchor=price_anchor,
            atr=atr,
        )
