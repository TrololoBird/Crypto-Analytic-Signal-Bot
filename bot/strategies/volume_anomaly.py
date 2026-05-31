"""Volume anomaly momentum setup.

# WINDSURF_REVIEW: unified + vectorized + 1H context + graded
"""

from __future__ import annotations

import math

from ..domain.config import BotSettings
from ..domain.schemas import PreparedSymbol, Signal
from ..setups.base import BaseSetup
from ..setups import _build_signal, _compute_dynamic_score, _reject
from ..setups.utils import get_dynamic_params
from ..setups.detectors import build_spec_signal, detect_volume_anomaly


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
            "min_volume_ratio": 1.35,
            "adaptive_volume_floor": 1.10,
            "min_body_atr": 0.25,
            "min_range_atr": 0.35,
            "min_close_position_long": 0.55,
            "max_close_position_short": 0.45,
            "signal_lookback_bars": 8,
            "close_hold_pct": 0.006,
            "min_wick_reclaim_atr": 0.25,
            "soft_volume_penalty": 0.90,
            "soft_body_penalty": 0.92,
            "soft_close_penalty": 0.94,
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
        signal_quality = 0.0
        score_penalty = 1.0
        configured_min_volume = float(effective_params.get("min_volume_ratio", 1.35))
        min_volume_ratio = max(1.0, min(configured_min_volume, 1.35))
        adaptive_volume_floor = max(
            1.0,
            min(float(effective_params.get("adaptive_volume_floor", 1.10)), min_volume_ratio),
        )
        min_body_atr = max(0.05, min(float(effective_params.get("min_body_atr", 0.25)), 0.35))
        min_range_atr = max(
            min_body_atr,
            min(float(effective_params.get("min_range_atr", 0.35)), 0.60),
        )
        min_close_long = min(
            max(float(effective_params.get("min_close_position_long", 0.55)), 0.50),
            0.60,
        )
        max_close_short = max(
            min(float(effective_params.get("max_close_position_short", 0.45)), 0.50),
            0.40,
        )
        close_hold_pct = max(
            0.0,
            min(float(effective_params.get("close_hold_pct", 0.006)), 0.015),
        )
        min_wick_reclaim_atr = max(
            0.05,
            min(float(effective_params.get("min_wick_reclaim_atr", 0.25)), 0.60),
        )
        signal_lookback = max(5, int(effective_params.get("signal_lookback_bars", 8)))
        recent = work.tail(min(signal_lookback, work.height))
        best_candidate: dict[str, float | str] | None = None
        for local_idx in range(recent.height - 1, -1, -1):
            open_ = _as_float(recent.item(local_idx, "open"))
            high = _as_float(recent.item(local_idx, "high"))
            low = _as_float(recent.item(local_idx, "low"))
            bar_close = _as_float(recent.item(local_idx, "close"))
            bar_vol_ratio = _as_float(recent.item(local_idx, "volume_ratio20"), 1.0)
            close_position = _as_float(recent.item(local_idx, "close_position"), 0.5)
            if min(open_, high, low, bar_close) <= 0.0 or high <= low:
                continue
            bar_range = high - low
            range_atr = bar_range / atr if atr > 0.0 else 0.0
            candidate_body_atr = abs(bar_close - open_) / atr if atr > 0.0 else 0.0
            lower_wick_atr = (min(open_, bar_close) - low) / atr if atr > 0.0 else 0.0
            upper_wick_atr = (high - max(open_, bar_close)) / atr if atr > 0.0 else 0.0
            volume_hard = bar_vol_ratio >= min_volume_ratio
            volume_soft = bar_vol_ratio >= adaptive_volume_floor
            if not (volume_hard or volume_soft):
                continue
            body_hard = candidate_body_atr >= min_body_atr
            range_hard = range_atr >= min_range_atr
            wick_reclaim_long = lower_wick_atr >= min_wick_reclaim_atr and close_position >= 0.50
            wick_reclaim_short = upper_wick_atr >= min_wick_reclaim_atr and close_position <= 0.50
            if not (body_hard or range_hard or wick_reclaim_long or wick_reclaim_short):
                continue
            midpoint = (high + low) / 2.0
            long_close_ok = close_position >= min_close_long
            short_close_ok = close_position <= max_close_short
            soft_long_close = close_position >= 0.52 or wick_reclaim_long
            soft_short_close = close_position <= 0.48 or wick_reclaim_short
            long_holds = close >= min(bar_close, midpoint) * (1.0 - close_hold_pct)
            short_holds = close <= max(bar_close, midpoint) * (1.0 + close_hold_pct)
            candidate_direction: str | None = None
            soft_close = False
            if bar_close >= open_ and long_holds and (long_close_ok or soft_long_close):
                candidate_direction = "long"
                soft_close = not long_close_ok
            elif bar_close <= open_ and short_holds and (short_close_ok or soft_short_close):
                candidate_direction = "short"
                soft_close = not short_close_ok
            elif wick_reclaim_long and long_holds:
                candidate_direction = "long"
                soft_close = not long_close_ok
            elif wick_reclaim_short and short_holds:
                candidate_direction = "short"
                soft_close = not short_close_ok
            if candidate_direction is None:
                continue

            quality = (
                min(bar_vol_ratio / max(min_volume_ratio, 1e-9), 1.8) * 0.36
                + min(max(candidate_body_atr, range_atr * 0.6) / max(min_body_atr, 1e-9), 1.8) * 0.34
                + (abs(close_position - 0.5) * 2.0) * 0.20
                + max(lower_wick_atr if candidate_direction == "long" else upper_wick_atr, 0.0) * 0.10
                - (recent.height - 1 - local_idx) * 0.025
            )
            candidate_penalty = 1.0
            if not volume_hard:
                candidate_penalty *= float(effective_params.get("soft_volume_penalty", 0.90))
            if not body_hard:
                candidate_penalty *= float(effective_params.get("soft_body_penalty", 0.92))
            if soft_close:
                candidate_penalty *= float(effective_params.get("soft_close_penalty", 0.94))
            if best_candidate is None or quality > float(best_candidate["quality"]):
                best_candidate = {
                    "direction": candidate_direction,
                    "quality": quality,
                    "penalty": candidate_penalty,
                    "idx": float(work.height - recent.height + local_idx),
                    "high": high,
                    "low": low,
                    "open": open_,
                    "close": bar_close,
                    "close_position": close_position,
                    "vol_ratio": max(vol_ratio, bar_vol_ratio),
                    "body_atr": candidate_body_atr,
                    "range_atr": range_atr,
                }

        if best_candidate is not None:
            direction = str(best_candidate["direction"])
            signal_idx = int(best_candidate["idx"])
            signal_high = float(best_candidate["high"])
            signal_low = float(best_candidate["low"])
            signal_open = float(best_candidate["open"])
            signal_close = float(best_candidate["close"])
            signal_close_position = float(best_candidate["close_position"])
            vol_ratio = float(best_candidate["vol_ratio"])
            body_atr = float(best_candidate["body_atr"])
            signal_quality = float(best_candidate["quality"])
            score_penalty = float(best_candidate["penalty"])

        if direction is None:
            _reject(
                prepared,
                setup_id,
                "candle_close_not_decisive",
                close_position=signal_close_position,
                rsi=rsi,
                min_volume_ratio=min_volume_ratio,
                adaptive_volume_floor=adaptive_volume_floor,
                lookback=signal_lookback,
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
            structure_clarity=min(max(body_atr / 1.2, signal_quality / 2.0), 1.0),
        )
        score *= score_penalty

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
            f"quality={signal_quality:.2f}",
            f"score_penalty={score_penalty:.2f}",
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
