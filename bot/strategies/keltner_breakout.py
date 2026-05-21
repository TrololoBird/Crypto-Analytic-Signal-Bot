"""Keltner channel breakout setup.

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
from .spec_patterns import build_spec_signal, detect_keltner_breakout


def _as_float(value: object, default: float = 0.0) -> float:
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    return default


class KeltnerBreakoutSetup(BaseSetup):
    setup_id = "keltner_breakout"
    family = "breakout"
    confirmation_profile = "breakout_acceptance"
    required_context = ("futures_flow",)

    def get_optimizable_params(self, settings: BotSettings | None = None) -> dict[str, float]:
        defaults = {
            "base_score": 0.54,
            "min_volume_ratio": 1.30,
            "min_adx_1h": 14.0,
            "sl_buffer_atr": 0.6,
            "min_rr": 1.9,
            "breakout_lookback_bars": 8,
            "recent_breakout_bars": 3,
            "kc_atr_mult": 1.80,
            "acceptance_band_pct": 0.01,
            "min_body_ratio": 0.45,
            "max_retest_distance_atr": 1.25,
            "volume_penalty": 0.90,
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
        hit = detect_keltner_breakout(
            prepared.work_15m,
            timeframe="15m",
            lookback_bars=int(effective_params.get("recent_breakout_bars", 3)),
            min_volume_ratio=float(effective_params.get("min_volume_ratio", 1.30)),
            kc_atr_mult=float(effective_params.get("kc_atr_mult", 1.80)),
            acceptance_band_pct=float(effective_params.get("acceptance_band_pct", 0.01)),
            min_body_ratio=float(effective_params.get("min_body_ratio", 0.45)),
        )
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
        work_15m = prepared.work_15m
        work_1h = prepared.work_1h
        if work_15m.height < 30 or work_1h.height < 30:
            _reject(prepared, setup_id, "insufficient_bars")
            return None

        required = (
            "close",
            "kc_upper",
            "kc_lower",
            "ema20",
            "atr14",
            "volume_ratio20",
            "rsi14",
        )
        missing = [column for column in required if column not in work_15m.columns]
        if "adx14" not in work_1h.columns:
            missing.append("adx14")
        if missing:
            _reject(prepared, setup_id, "missing_columns", missing_fields=missing)
            return None

        params = self.get_optimizable_params(settings)
        dynamic_params = get_dynamic_params(prepared, setup_id)
        effective_params = {**params, **dynamic_params}

        close = _as_float(work_15m.item(-1, "close"))
        ema20 = _as_float(work_15m.item(-1, "ema20"))
        atr = _as_float(work_15m.item(-1, "atr14"))
        vol_ratio = _as_float(work_15m.item(-1, "volume_ratio20"), 1.0)
        rsi = _as_float(work_15m.item(-1, "rsi14"), 50.0)
        adx_1h = _as_float(work_1h.item(-1, "adx14"))

        if min(close, ema20, atr) <= 0.0 or math.isnan(atr):
            _reject(prepared, setup_id, "invalid_indicator_state", atr=atr)
            return None

        if adx_1h > 0.0 and adx_1h < float(effective_params["min_adx_1h"]):
            _reject(prepared, setup_id, "adx_too_low", adx_1h=adx_1h)
            return None

        bias_1h = getattr(prepared, "bias_1h", prepared.bias_4h)
        direction: str | None = None
        stop_basis: float = 0.0
        entry_price = 0.0
        breakout_lag = 0
        breakout_vol_ratio = vol_ratio
        kc_atr_mult = max(1.0, min(float(effective_params.get("kc_atr_mult", 1.80)), 3.0))
        min_volume_ratio = float(effective_params.get("min_volume_ratio", 1.30))
        min_body_ratio = max(0.0, min(float(effective_params.get("min_body_ratio", 0.45)), 1.0))
        acceptance_band = max(
            0.0,
            min(float(effective_params.get("acceptance_band_pct", 0.01)), 0.05),
        )
        lookback = min(
            max(1, int(effective_params.get("recent_breakout_bars", 3))),
            work_15m.height,
        )
        current_upper = ema20 + kc_atr_mult * atr
        current_lower = ema20 - kc_atr_mult * atr
        current_holds_long = close >= current_upper * (1.0 - acceptance_band)
        current_holds_short = close <= current_lower * (1.0 + acceptance_band)
        for idx in range(work_15m.height - 1, work_15m.height - lookback - 1, -1):
            bar_open = _as_float(work_15m.item(idx, "open"))
            bar_high = _as_float(work_15m.item(idx, "high"))
            bar_low = _as_float(work_15m.item(idx, "low"))
            bar_close = _as_float(work_15m.item(idx, "close"))
            bar_ema = _as_float(work_15m.item(idx, "ema20"))
            bar_atr = _as_float(work_15m.item(idx, "atr14"))
            bar_vol = _as_float(work_15m.item(idx, "volume_ratio20"), 1.0)
            if min(bar_open, bar_high, bar_low, bar_close, bar_ema, bar_atr) <= 0.0:
                continue
            if bar_vol < min_volume_ratio:
                continue
            bar_range = max(bar_high - bar_low, 1e-9)
            body_ratio = abs(bar_close - bar_open) / bar_range
            if body_ratio < min_body_ratio:
                continue
            bar_upper = bar_ema + kc_atr_mult * bar_atr
            bar_lower = bar_ema - kc_atr_mult * bar_atr
            if current_holds_long and bar_close > bar_upper and bar_close > bar_open:
                direction = "long"
                entry_price = max(current_upper, bar_upper)
                stop_basis = min(bar_low, bar_ema)
                breakout_lag = work_15m.height - 1 - idx
                breakout_vol_ratio = bar_vol
                break
            if current_holds_short and bar_close < bar_lower and bar_close < bar_open:
                direction = "short"
                entry_price = min(current_lower, bar_lower)
                stop_basis = max(bar_high, bar_ema)
                breakout_lag = work_15m.height - 1 - idx
                breakout_vol_ratio = bar_vol
                break

        if direction is None:
            _reject(
                prepared,
                setup_id,
                "pattern.no_keltner_breakout",
                close=close,
                kc_upper=current_upper,
                kc_lower=current_lower,
                min_volume_ratio=min_volume_ratio,
                lookback=lookback,
            )
            return None

        sh_mask, sl_mask = _swing_points(work_1h, n=3, include_unconfirmed_tail=True)
        min_rr = float(effective_params["min_rr"])
        stop, tp1, tp2 = build_structural_targets(
            direction=direction,
            price_anchor=entry_price,
            stop_basis=stop_basis,
            atr=atr,
            work_1h=work_1h,
            work_4h=prepared.work_4h,
            min_rr=min_rr,
            sl_buffer_atr=float(effective_params["sl_buffer_atr"]),
            sh_mask=sh_mask,
            sl_mask=sl_mask,
        )
        if direction == "long" and stop >= entry_price:
            stop = entry_price - atr * float(effective_params["sl_buffer_atr"])
        elif direction == "short" and stop <= entry_price:
            stop = entry_price + atr * float(effective_params["sl_buffer_atr"])
        risk = abs(entry_price - stop)
        if risk <= 0.0:
            _reject(prepared, setup_id, "invalid_stop", stop=stop, close=entry_price)
            return None
        if tp1 is None or abs(tp1 - entry_price) < risk * min_rr:
            tp1 = (
                entry_price + risk * min_rr
                if direction == "long"
                else entry_price - risk * min_rr
            )
        if tp2 is None or abs(tp2 - entry_price) <= abs(tp1 - entry_price):
            tp2 = (
                entry_price + risk * max(2.0, min_rr + 0.35)
                if direction == "long"
                else entry_price - risk * max(2.0, min_rr + 0.35)
            )

        base_score = float(effective_params["base_score"])
        score = _compute_dynamic_score(
            direction=direction,
            base_score=base_score,
            vol_ratio=max(vol_ratio, breakout_vol_ratio),
            rsi=rsi,
            structure_clarity=0.6,
        )

        # Graded bias alignment
        if direction == "long" and bias_1h == "downtrend":
            score *= effective_params.get("bias_mismatch_penalty", 0.75)
        elif direction == "short" and bias_1h == "uptrend":
            score *= effective_params.get("bias_mismatch_penalty", 0.75)

        reasons = [
            f"keltner_breakout_{direction}",
            f"bias_1h={bias_1h}",
            f"limit_entry={entry_price:.4f}",
            f"breakout_lag={breakout_lag}",
            f"vol_ratio={vol_ratio:.2f} breakout_vol={breakout_vol_ratio:.2f}",
            f"kc_mult={kc_atr_mult:.2f} acceptance_band={acceptance_band:.3f}",
            f"adx_1h={adx_1h:.1f}",
        ]
        return _build_signal(
            prepared=prepared,
            setup_id=setup_id,
            direction=direction,
            score=score,
            timeframe="15m+1h",
            reasons=reasons,
            strategy_family=self.family,
            stop=stop,
            tp1=tp1,
            tp2=tp2,
            price_anchor=entry_price,
            atr=atr,
        )
