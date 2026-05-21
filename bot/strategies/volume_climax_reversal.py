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
from .spec_patterns import build_spec_signal, detect_volume_climax_reversal


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
            "signal_lookback_bars": 10,
            "body_reversal_close_position_long": 0.62,
            "body_reversal_close_position_short": 0.38,
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
        params = self.get_optimizable_params(settings)
        dynamic_params = get_dynamic_params(prepared, setup_id)
        effective_params = {**params, **dynamic_params}
        hit = detect_volume_climax_reversal(work, timeframe="15m")
        if hit is None:
            _reject(prepared, setup_id, "pattern.no_volume_climax_reclaim")
            return None
        return build_spec_signal(
            prepared=prepared,
            settings=settings,
            setup_id=setup_id,
            family=self.family,
            hit=hit,
            defaults=params,
            params=effective_params,
        )

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

        atr = _as_float(work.item(-1, "atr14"))
        close = _as_float(work.item(-1, "close"))
        latest_close_position = _as_float(work.item(-1, "close_position"), 0.5)
        rsi = _as_float(work.item(-1, "rsi14"), 50.0)

        if min(close, atr) <= 0.0 or math.isnan(atr):
            _reject(prepared, setup_id, "invalid_indicator_state", atr=atr)
            return None

        min_wick_atr = float(effective_params["min_wick_atr"])
        configured_min_volume = float(effective_params["min_volume_ratio"])
        adaptive_min_volume = float(
            effective_params.get("adaptive_min_volume_ratio", 1.30)
        )
        strong_wick_multiplier = float(
            effective_params.get("strong_wick_multiplier", 1.35)
        )
        direction: str | None = None
        clarity = 0.0
        signal_lookback = max(
            1,
            int(effective_params.get("signal_lookback_bars", 3)),
        )
        recent = work.tail(min(signal_lookback, work.height))
        signal_idx = work.height - 1
        signal_high = _as_float(work.item(-1, "high"))
        signal_low = _as_float(work.item(-1, "low"))
        signal_vol_ratio = _as_float(work.item(-1, "volume_ratio20"), 1.0)
        lower_wick_atr = 0.0
        upper_wick_atr = 0.0
        reclaim_level = 0.0
        for local_idx in range(recent.height - 1, -1, -1):
            open_ = _as_float(recent.item(local_idx, "open"))
            high = _as_float(recent.item(local_idx, "high"))
            low = _as_float(recent.item(local_idx, "low"))
            bar_close = _as_float(recent.item(local_idx, "close"))
            prev_low = _as_float(recent.item(local_idx, "prev_donchian_low20"))
            prev_high = _as_float(recent.item(local_idx, "prev_donchian_high20"))
            close_position = _as_float(recent.item(local_idx, "close_position"), 0.5)
            vol_ratio_candidate = _as_float(recent.item(local_idx, "volume_ratio20"), 1.0)
            if min(open_, high, low, bar_close, prev_low, prev_high) <= 0.0:
                continue
            candidate_lower_wick_atr = (min(open_, bar_close) - low) / atr
            candidate_upper_wick_atr = (high - max(open_, bar_close)) / atr
            long_reclaim = max(bar_close, close) > prev_low and latest_close_position >= 0.52
            short_reclaim = min(bar_close, close) < prev_high and latest_close_position <= 0.48
            if (
                low < prev_low
                and long_reclaim
                and candidate_lower_wick_atr >= min_wick_atr
                and close_position >= 0.48
            ):
                direction = "long"
                clarity = min(candidate_lower_wick_atr / 2.0, 1.0)
                lower_wick_atr = candidate_lower_wick_atr
                upper_wick_atr = candidate_upper_wick_atr
                signal_idx = work.height - recent.height + local_idx
                signal_low = low
                signal_high = high
                signal_vol_ratio = max(signal_vol_ratio, vol_ratio_candidate)
                reclaim_level = prev_low
                break
            if (
                candidate_lower_wick_atr >= min_wick_atr * 1.35
                and close >= bar_close * 0.996
                and close_position >= 0.45
                and rsi <= float(effective_params["max_rsi_long"]) + 15.0
            ):
                direction = "long"
                clarity = min(candidate_lower_wick_atr / 2.0, 1.0)
                lower_wick_atr = candidate_lower_wick_atr
                upper_wick_atr = candidate_upper_wick_atr
                signal_idx = work.height - recent.height + local_idx
                signal_low = low
                signal_high = high
                signal_vol_ratio = max(signal_vol_ratio, vol_ratio_candidate)
                reclaim_level = prev_low
                break
            if (
                high > prev_high
                and short_reclaim
                and candidate_upper_wick_atr >= min_wick_atr
                and close_position <= 0.52
            ):
                direction = "short"
                clarity = min(candidate_upper_wick_atr / 2.0, 1.0)
                lower_wick_atr = candidate_lower_wick_atr
                upper_wick_atr = candidate_upper_wick_atr
                signal_idx = work.height - recent.height + local_idx
                signal_low = low
                signal_high = high
                signal_vol_ratio = max(signal_vol_ratio, vol_ratio_candidate)
                reclaim_level = prev_high
                break
            if (
                candidate_upper_wick_atr >= min_wick_atr * 1.35
                and close <= bar_close * 1.004
                and close_position <= 0.55
                and rsi >= float(effective_params["min_rsi_short"]) - 15.0
            ):
                direction = "short"
                clarity = min(candidate_upper_wick_atr / 2.0, 1.0)
                lower_wick_atr = candidate_lower_wick_atr
                upper_wick_atr = candidate_upper_wick_atr
                signal_idx = work.height - recent.height + local_idx
                signal_low = low
                signal_high = high
                signal_vol_ratio = max(signal_vol_ratio, vol_ratio_candidate)
                reclaim_level = prev_high
                break

        if direction is None:
            best_idx = -1
            best_vol = 0.0
            for local_idx in range(recent.height - 1, -1, -1):
                candidate_vol = _as_float(recent.item(local_idx, "volume_ratio20"), 1.0)
                if candidate_vol >= best_vol:
                    best_vol = candidate_vol
                    best_idx = local_idx
            if best_idx >= 0 and best_vol >= adaptive_min_volume:
                open_ = _as_float(recent.item(best_idx, "open"))
                high = _as_float(recent.item(best_idx, "high"))
                low = _as_float(recent.item(best_idx, "low"))
                bar_close = _as_float(recent.item(best_idx, "close"))
                close_position = _as_float(recent.item(best_idx, "close_position"), 0.5)
                prev_low = _as_float(recent.item(best_idx, "prev_donchian_low20"))
                prev_high = _as_float(recent.item(best_idx, "prev_donchian_high20"))
                body = abs(bar_close - open_)
                bar_range = max(high - low, atr)
                last_delta = close - bar_close
                lower_wick_atr = (min(open_, bar_close) - low) / atr
                upper_wick_atr = (high - max(open_, bar_close)) / atr
                body_reversal_long = (
                    close_position
                    >= float(effective_params["body_reversal_close_position_long"])
                    and bar_close >= open_
                    and last_delta >= -atr * 0.20
                    and body / bar_range >= 0.20
                )
                body_reversal_short = (
                    close_position
                    <= float(effective_params["body_reversal_close_position_short"])
                    and bar_close <= open_
                    and last_delta <= atr * 0.20
                    and body / bar_range >= 0.20
                )
                if (lower_wick_atr >= min_wick_atr or body_reversal_long) and rsi <= 58.0:
                    direction = "long"
                    clarity = min(max(lower_wick_atr, body / bar_range) / 2.0, 1.0)
                    signal_low = low
                    signal_high = high
                    signal_vol_ratio = max(signal_vol_ratio, best_vol)
                    signal_idx = work.height - recent.height + best_idx
                    reclaim_level = prev_low
                elif (upper_wick_atr >= min_wick_atr or body_reversal_short) and rsi >= 42.0:
                    direction = "short"
                    clarity = min(max(upper_wick_atr, body / bar_range) / 2.0, 1.0)
                    signal_low = low
                    signal_high = high
                    signal_vol_ratio = max(signal_vol_ratio, best_vol)
                    signal_idx = work.height - recent.height + best_idx
                    reclaim_level = prev_high
            if direction is None:
                last_open = _as_float(work.item(-1, "open"))
                last_high = _as_float(work.item(-1, "high"))
                last_low = _as_float(work.item(-1, "low"))
                last_close = _as_float(work.item(-1, "close"))
                last_lower_wick_atr = (min(last_open, last_close) - last_low) / atr
                last_upper_wick_atr = (last_high - max(last_open, last_close)) / atr
                _reject(
                    prepared,
                    setup_id,
                    "no_volume_climax_reclaim",
                    lower_wick_atr=last_lower_wick_atr,
                    upper_wick_atr=last_upper_wick_atr,
                    rsi=rsi,
                    best_volume_ratio=best_vol,
                )
                return None

        signal_wick_atr = lower_wick_atr if direction == "long" else upper_wick_atr
        strong_wick = signal_wick_atr >= (min_wick_atr * strong_wick_multiplier)
        required_volume = configured_min_volume
        if strong_wick:
            required_volume = min(configured_min_volume, adaptive_min_volume)
        vol_ratio = signal_vol_ratio
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
        signal_mid = (signal_high + signal_low) / 2.0
        if reclaim_level > 0.0:
            price_anchor = reclaim_level
        elif direction == "long":
            price_anchor = min(signal_mid, close)
        else:
            price_anchor = max(signal_mid, close)
        if direction == "long":
            stop = signal_low - atr * sl_buffer
            risk = price_anchor - stop
            tp1 = price_anchor + risk * min_rr
            tp2 = price_anchor + risk * max(2.0, min_rr + 0.35)
        else:
            stop = signal_high + atr * sl_buffer
            risk = stop - price_anchor
            tp1 = price_anchor - risk * min_rr
            tp2 = price_anchor - risk * max(2.0, min_rr + 0.35)
        if risk <= 0.0:
            _reject(prepared, setup_id, "invalid_stop", stop=stop, close=price_anchor)
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
            f"signal_lag={work.height - 1 - signal_idx}",
            f"reclaim_level={reclaim_level:.4f}",
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
