"""Keltner channel breakout setup.

# WINDSURF_REVIEW: unified + vectorized + 1H context + graded
"""

from __future__ import annotations


from ..config import BotSettings
from ..features import _swing_points
from ..models import PreparedSymbol, Signal
from ..setup_base import BaseSetup
from ..setups import _build_standard_signal, _reject, as_float
from ..setups.utils import (
    build_structural_targets,
    get_merged_params,
    validate_prepared_data,
)


class KeltnerBreakoutSetup(BaseSetup):
    setup_id = "keltner_breakout"
    family = "breakout"
    confirmation_profile = "breakout_acceptance"
    required_context = ("futures_flow",)

    def get_optimizable_params(
        self, settings: BotSettings | None = None
    ) -> dict[str, float]:
        defaults = {
            "base_score": 0.54,
            "min_volume_ratio": 1.25,
            "min_adx_1h": 14.0,
            "sl_buffer_atr": 0.6,
            "min_rr": 1.5,
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

        params = get_merged_params(
            setup_id, self.get_optimizable_params(settings), settings, prepared
        )
        if not validate_prepared_data(
            prepared, setup_id, required_columns=required, min_bars=30
        ):
            return None

        close = as_float(work_15m.item(-1, "close"))
        kc_upper = as_float(work_15m.item(-1, "kc_upper"))
        kc_lower = as_float(work_15m.item(-1, "kc_lower"))
        ema20 = as_float(work_15m.item(-1, "ema20"))
        atr = as_float(work_15m.item(-1, "atr14"))
        vol_ratio = as_float(work_15m.item(-1, "volume_ratio20"), 1.0)
        rsi = as_float(work_15m.item(-1, "rsi14"), 50.0)
        adx_1h = as_float(work_1h.item(-1, "adx14"))

        if min(close, kc_upper, kc_lower, ema20, atr) <= 0.0:
            _reject(prepared, setup_id, "invalid_indicator_state", atr=atr)
            return None

        if vol_ratio < params["min_volume_ratio"]:
            _reject(prepared, setup_id, "volume_too_low", volume_ratio=vol_ratio)
            return None

        if adx_1h > 0.0 and adx_1h < params["min_adx_1h"]:
            _reject(prepared, setup_id, "adx_too_low", adx_1h=adx_1h)
            return None

        direction: str | None = None
        stop_basis: float = 0.0

        if close > kc_upper:
            direction = "long"
            stop_basis = max(kc_lower, ema20)
        elif close < kc_lower:
            direction = "short"
            stop_basis = min(kc_upper, ema20)

        if direction is None:
            _reject(prepared, setup_id, "no_keltner_breakout", close=close)
            return None

        sh_mask, sl_mask = _swing_points(work_1h, n=3, include_unconfirmed_tail=True)
        min_rr = params["min_rr"]
        stop, tp1, tp2 = build_structural_targets(
            direction=direction,
            price_anchor=close,
            stop_basis=stop_basis,
            atr=atr,
            work_1h=work_1h,
            work_4h=prepared.work_4h,
            min_rr=min_rr,
            sl_buffer_atr=params["sl_buffer_atr"],
            sh_mask=sh_mask,
            sl_mask=sl_mask,
        )
        risk = abs(close - stop)
        if risk <= 0.0:
            _reject(prepared, setup_id, "invalid_stop", stop=stop, close=close)
            return None
        if tp1 is None or abs(tp1 - close) < risk * min_rr:
            tp1 = (
                close + risk * min_rr if direction == "long" else close - risk * min_rr
            )
        if tp2 is None:
            tp2 = tp1

        return _build_standard_signal(
            prepared=prepared,
            setup_id=setup_id,
            direction=direction,
            params=params,
            vol_ratio=vol_ratio,
            rsi=rsi,
            structure_clarity=0.6,
            stop=stop,
            tp1=tp1,
            tp2=tp2,
            price_anchor=close,
            atr=atr,
            timeframe="15m+1h",
            strategy_family=self.family,
            extra_reasons=[
                f"vol_ratio={vol_ratio:.2f}",
                f"adx_1h={adx_1h:.1f}",
            ],
        )
