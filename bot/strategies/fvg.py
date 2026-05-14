"""Fair Value Gap (FVG) setup detector.

Bullish FVG: high[i-2] < low[i]  (gap between candle i-2 top and candle i bottom)
Bearish FVG: low[i-2] > high[i]

Scans last 50 bars of work_15m; enters when price is currently inside a gap.

# WINDSURF_REVIEW: unified + vectorized + 1H context + graded
"""

from __future__ import annotations

import logging
import math

from ..setup_base import BaseSetup
from ..domain.config import BotSettings
from ..domain.schemas import PreparedSymbol, Signal
from ..setups import _build_signal, _compute_dynamic_score, _reject
from ..setups.smc import latest_fvg_zone
from ..setups.utils import (
    validate_rr_or_penalty,
    get_dynamic_params,
)

LOG = logging.getLogger("bot.strategies.fvg")


class FVGSetup(BaseSetup):
    setup_id = "fvg_setup"
    family = "continuation"
    confirmation_profile = "trend_follow"
    required_context = ("futures_flow",)

    def get_optimizable_params(self, settings: BotSettings | None = None) -> dict[str, float]:
        """Tunable parameters for self-learner optimization."""
        defaults = {
            "base_score": 0.55,
            "min_gap_width_bps": 15.0,
            "min_volume_ratio": 1.10,
            "bias_mismatch_penalty": 0.75,
            "rsi_overbought": 70.0,
            "rsi_oversold": 30.0,
            "min_rr": 1.5,
            "tp_too_close_penalty": 0.8,
            "min_fvg_size_atr": 0.25,
            "min_mitigation_pct": 0.20,
            "sl_buffer_atr": 0.50,
        }
        if settings is not None:
            filters = getattr(settings, "filters", None)
            if filters:
                setups_config = getattr(filters, "setups", {})
                if isinstance(setups_config, dict):
                    override = setups_config.get(self.setup_id) or setups_config.get("fvg")
                    if isinstance(override, dict):
                        return {**defaults, **override}
        return defaults

    def detect(self, prepared: PreparedSymbol, settings: BotSettings) -> Signal | None:
        try:
            return self._detect(prepared, settings)
        except Exception as exc:
            LOG.exception("%s fvg_setup: unexpected error", prepared.symbol)
            _reject(
                prepared,
                self.setup_id,
                "runtime.unexpected_exception",
                stage="runtime",
                exception_type=type(exc).__name__,
            )
            return None

    def _detect(self, prepared: PreparedSymbol, settings: BotSettings) -> Signal | None:
        setup_id = self.setup_id
        dynamic_params = get_dynamic_params(prepared, setup_id)
        defaults = self.get_optimizable_params(settings)

        min_fvg_size_atr = dynamic_params.get("min_fvg_size_atr", defaults["min_fvg_size_atr"])
        min_mitigation_pct = dynamic_params.get(
            "min_mitigation_pct", defaults["min_mitigation_pct"]
        )
        sl_buffer_atr = dynamic_params.get("sl_buffer_atr", defaults["sl_buffer_atr"])

        w = prepared.work_15m
        if w.height < 5:
            _reject(prepared, setup_id, "insufficient_15m_bars", bars=w.height)
            return None

        atr = float(w.item(-1, "atr14") or 0.0)
        if atr <= 0 or math.isnan(atr):
            _reject(prepared, setup_id, "atr_invalid", atr=atr)
            return None

        price = prepared.mark_price or prepared.universe.last_price
        if not price or price <= 0:
            _reject(prepared, setup_id, "price_missing")
            return None

        vol_ratio = float(w.item(-1, "volume_ratio20") or 1.0)
        rsi = float(w.item(-1, "rsi14") or 50.0)

        min_gap_width_bps = dynamic_params.get("min_gap_width_bps", defaults["min_gap_width_bps"])
        min_volume_ratio = dynamic_params.get("min_volume_ratio", defaults["min_volume_ratio"])
        price_touch_buffer = max(atr * 0.1, price * 0.001)
        zone = latest_fvg_zone(
            w,
            join_consecutive=True,
            allowed_states=("fresh",),
            current_price=price,
            touch_buffer=price_touch_buffer,
        )
        if zone is None:
            _reject(prepared, setup_id, "no_fvg_detected")
            return None

        direction = zone.direction
        fvg_low = zone.bottom
        fvg_high = zone.top
        fvg_width = zone.width
        fvg_mid = zone.midpoint
        try:
            zone_values_valid = all(
                math.isfinite(float(value)) and float(value) > 0.0
                for value in (fvg_low, fvg_high, fvg_width, fvg_mid)
            )
        except (TypeError, ValueError):
            zone_values_valid = False
        if (
            direction not in {"long", "short"}
            or zone.created_index is None
            or not (0 <= int(zone.created_index) < w.height)
            or not zone_values_valid
        ):
            _reject(
                prepared,
                setup_id,
                "invalid_fvg_zone",
                direction=direction,
                top=fvg_high,
                bottom=fvg_low,
                width=fvg_width,
                created_index=zone.created_index,
            )
            return None
        mitigation_pct = abs(price - fvg_mid) / fvg_width if fvg_width > 0 else 0.0
        if (
            fvg_width / price < (min_gap_width_bps / 10000)
            or fvg_width < atr * float(min_fvg_size_atr)
            or not (float(min_mitigation_pct) <= mitigation_pct <= 1.0)
        ):
            _reject(
                prepared,
                setup_id,
                "fvg_constraints_failed",
                width=fvg_width,
                mitigation_pct=mitigation_pct,
            )
            return None

        impulse_vol_ratio = 1.0
        if "volume_ratio20" in w.columns:
            try:
                raw_vol_ratio = w.item(zone.created_index, "volume_ratio20")
                impulse_vol_ratio = float(raw_vol_ratio) if raw_vol_ratio is not None else 1.0
            except (IndexError, TypeError, ValueError):
                impulse_vol_ratio = 1.0
        if impulse_vol_ratio < min_volume_ratio:
            _reject(
                prepared,
                setup_id,
                "impulse_volume_below_threshold",
                impulse_vol_ratio=impulse_vol_ratio,
                min_volume_ratio=min_volume_ratio,
            )
            return None

        # Use 1H context for 15M signals (not 4H - too lagging for <4h trades)
        bias_1h = getattr(prepared, "bias_1h", prepared.bias_4h)
        regime_1h = getattr(prepared, "regime_1h_confirmed", prepared.regime_4h_confirmed)

        # Graded scoring instead of hard reject for bias mismatch
        base_score = dynamic_params.get("base_score", defaults["base_score"])
        score = _compute_dynamic_score(
            direction=direction,
            base_score=base_score,
            vol_ratio=vol_ratio,
            rsi=rsi,
        )

        if direction == "long" and bias_1h == "downtrend":
            score *= dynamic_params.get("bias_mismatch_penalty", defaults["bias_mismatch_penalty"])
        if direction == "short" and bias_1h == "uptrend":
            score *= dynamic_params.get("bias_mismatch_penalty", defaults["bias_mismatch_penalty"])

        # RSI extremes filter with graded penalty
        rsi_overbought = dynamic_params.get("rsi_overbought", defaults["rsi_overbought"])
        rsi_oversold = dynamic_params.get("rsi_oversold", defaults["rsi_oversold"])
        if direction == "long" and rsi > rsi_overbought:
            score *= 0.85  # Light penalty for overbought
        if direction == "short" and rsi < rsi_oversold:
            score *= 0.85  # Light penalty for oversold

        # 1h structure alignment with graded penalty
        structure_1h = prepared.structure_1h
        if direction == "long" and structure_1h == "downtrend":
            score *= dynamic_params.get("bias_mismatch_penalty", defaults["bias_mismatch_penalty"])
        if direction == "short" and structure_1h == "uptrend":
            score *= dynamic_params.get(
                "bias_mismatch_penalty", defaults["bias_mismatch_penalty"]
            )
        # --- Compute structural SL/TP ---
        if direction == "long":
            # SL: beyond opposite side of FVG + 0.5×ATR (was 0.1)
            stop = fvg_low - atr * float(sl_buffer_atr)
            # TP1: FVG midpoint (50% fill)
            tp1 = fvg_mid if fvg_mid > price else fvg_high
            # TP2: full FVG fill (opposite boundary)
            tp2 = fvg_high
            if tp1 <= price:
                tp1 = price + max(fvg_width, atr * 0.5)
            if tp2 <= tp1:
                tp2 = tp1 + max(fvg_width * 0.5, atr * 0.5)
        else:
            # SL: beyond opposite side of FVG + 0.5×ATR (was 0.1)
            stop = fvg_high + atr * float(sl_buffer_atr)
            # TP1: FVG midpoint (50% fill)
            tp1 = fvg_mid if fvg_mid < price else fvg_low
            # TP2: full FVG fill
            tp2 = fvg_low
            if tp1 >= price:
                tp1 = price - max(fvg_width, atr * 0.5)
            if tp2 >= tp1:
                tp2 = tp1 - max(fvg_width * 0.5, atr * 0.5)

        # Graded RR validation instead of hard reject
        min_rr = dynamic_params.get("min_rr", defaults["min_rr"])
        is_valid_rr, _ = validate_rr_or_penalty(price, stop, tp1, min_rr)

        if not is_valid_rr and tp1 is not None:
            score *= dynamic_params.get("tp_too_close_penalty", defaults["tp_too_close_penalty"])

        if tp2 is None or abs(tp2 - price) <= abs(tp1 - price):
            tp2 = tp1  # Use TP1 as TP2 if no extended target found

        reasons = [
            f"FVG {direction}: gap [{fvg_low:.4f}-{fvg_high:.4f}] state={zone.state}",
            f"price={price:.4f} inside gap | 1h_bias={bias_1h} 1h_struct={structure_1h} 1h_regime={regime_1h}",
            f"vol_ratio={vol_ratio:.2f} impulse_vol={impulse_vol_ratio:.2f} rsi={rsi:.1f}",
        ]

        return _build_signal(
            prepared=prepared,
            setup_id=self.setup_id,
            direction=direction,
            score=score,
            timeframe="15m",
            reasons=reasons,
            strategy_family=self.family,
            stop=stop,
            tp1=tp1,
            tp2=tp2,
            price_anchor=price,
            atr=atr,
        )
