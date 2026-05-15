"""CVD Delta Divergence setup detector.

Uses delta_ratio column (CVD proxy) in work_15m if available.
Detects divergence between price direction and order flow delta.

# WINDSURF_REVIEW: unified + vectorized + 1H context + graded
"""

from __future__ import annotations

import logging
from typing import Any, cast
import math

from ..domain.config import BotSettings
from ..features import _swing_points
from ..domain.schemas import PreparedSymbol, Signal
from ..setup_base import BaseSetup
from ..setups import _build_signal, _compute_dynamic_score, _reject
from ..setups.utils import get_dynamic_params

LOG = logging.getLogger("bot.strategies.cvd_divergence")


def _signed_delta_values(values: Any) -> Any:
    """Normalize common public-flow encodings to signed delta [-1, 1]."""
    raw: Any = values
    try:
        min_value = float(raw.min())
        max_value = float(raw.max())
    except (AttributeError, TypeError, ValueError):
        return raw
    if min_value < 0.0:
        return raw
    if max_value > 1.0:
        return (raw - 1.0) / (raw + 1.0)
    return (raw - 0.5) * 2.0


class CVDDivergenceSetup(BaseSetup):
    setup_id = "cvd_divergence"
    family = "reversal"
    confirmation_profile = "countertrend_exhaustion"
    required_context = ("futures_flow",)

    def get_optimizable_params(self, settings: BotSettings | None = None) -> dict[str, float]:
        """Tunable parameters for self-learner optimization."""
        defaults = {
            "base_score": 0.50,
            "divergence_lookback": 5,
            "delta_lookback": 3,
            "bias_mismatch_penalty": 0.75,
            "min_rr": 1.9,
            "min_delta_threshold": 0.06,
            "sl_buffer_atr": 0.5,
        }
        if settings is not None:
            filters = getattr(settings, "filters", None)
            if filters:
                setups_config = getattr(filters, "setups", {})
                if isinstance(setups_config, dict) and self.setup_id in setups_config:
                    return {**defaults, **setups_config.get(self.setup_id, {})}
        return defaults

    def detect(self, prepared: PreparedSymbol, settings: BotSettings) -> Signal | None:
        try:
            return self._detect(prepared, settings)
        except Exception as exc:
            LOG.exception("%s cvd_divergence: unexpected error", prepared.symbol)
            _reject(
                prepared,
                self.setup_id,
                "runtime.unexpected_exception",
                stage="runtime",
                exception_type=type(exc).__name__,
            )
            return None

    def _detect(self, prepared: PreparedSymbol, settings: BotSettings) -> Signal | None:
        dynamic_params = get_dynamic_params(prepared, self.setup_id)
        defaults = self.get_optimizable_params(settings)
        divergence_lookback = int(
            dynamic_params.get("divergence_lookback", defaults["divergence_lookback"])
        )
        min_delta_threshold = float(
            dynamic_params.get("min_delta_threshold", defaults["min_delta_threshold"])
        )
        sl_buffer_atr = float(dynamic_params.get("sl_buffer_atr", defaults["sl_buffer_atr"]))
        min_rr = float(dynamic_params.get("min_rr", defaults["min_rr"]))
        base_score = float(dynamic_params.get("base_score", defaults["base_score"]))

        w = prepared.work_15m
        if w.height < 20:
            _reject(prepared, self.setup_id, "insufficient_15m_bars", bars=w.height)
            return None

        if "delta_ratio" not in w.columns:
            _reject(prepared, self.setup_id, "delta_ratio_missing")
            return None
        delta_series = w["delta_ratio"].drop_nulls()
        if delta_series.len() < 10:
            _reject(
                prepared,
                self.setup_id,
                "delta_history_insufficient",
                samples=delta_series.len(),
            )
            return None

        atr = float(w.item(-1, "atr14") or 0.0)
        if atr <= 0 or math.isnan(atr):
            _reject(prepared, self.setup_id, "atr_invalid", atr=atr)
            return None

        price = prepared.mark_price or prepared.universe.last_price
        if not price or price <= 0:
            _reject(prepared, self.setup_id, "price_missing")
            return None

        closes = w["close"].to_numpy()
        highs = w["high"].to_numpy()
        lows = w["low"].to_numpy()
        delta_vals = _signed_delta_values(w["delta_ratio"].to_numpy())

        split = max(2, divergence_lookback)
        compare = split * 2
        window_a = closes[-compare:-split]
        window_b = closes[-split:]
        high_window_b = highs[-split:]
        low_window_b = lows[-split:]
        delta_a = delta_vals[-compare:-split]
        delta_b = delta_vals[-split:]

        if len(window_a) < 5 or len(window_b) < 5:
            _reject(prepared, self.setup_id, "divergence_window_insufficient")
            return None

        price_hh = float(max(window_b)) > float(max(window_a))
        price_ll = float(min(window_b)) < float(min(window_a))
        delta_mean_a = float(cast(Any, delta_a).mean())
        delta_mean_b = float(cast(Any, delta_b).mean())
        delta_shift = delta_mean_b - delta_mean_a

        if math.isnan(delta_mean_a) or math.isnan(delta_mean_b):
            _reject(
                prepared,
                self.setup_id,
                "delta_mean_invalid",
                delta_mean_a=delta_mean_a,
                delta_mean_b=delta_mean_b,
            )
            return None
        if abs(delta_shift) < min_delta_threshold:
            _reject(
                prepared,
                self.setup_id,
                "delta_shift_too_small",
                delta_shift=delta_shift,
                min_delta_threshold=min_delta_threshold,
            )
            return None

        direction = None

        # Use 1H context for 15M signals (not 4H - too lagging for <4h trades)
        bias_1h = getattr(prepared, "bias_1h", prepared.bias_4h)

        # Bearish divergence: price HH, delta declining
        if price_hh and delta_mean_b < delta_mean_a:
            # Don't short in 1H uptrend unless delta very extreme
            bias_override_threshold = max(0.2, min_delta_threshold)
            if bias_1h == "uptrend" and delta_shift > -bias_override_threshold:
                _reject(
                    prepared,
                    self.setup_id,
                    "context_bias_blocks_short",
                    bias_1h=bias_1h,
                    delta_shift=delta_shift,
                )
                return None
            direction = "short"

        # Bullish divergence: price LL, delta rising
        elif price_ll and delta_mean_b > delta_mean_a:
            bias_override_threshold = max(0.2, min_delta_threshold)
            if bias_1h == "downtrend" and delta_shift < bias_override_threshold:
                _reject(
                    prepared,
                    self.setup_id,
                    "context_bias_blocks_long",
                    bias_1h=bias_1h,
                    delta_shift=delta_shift,
                )
                return None
            direction = "long"

        if direction is None:
            _reject(prepared, self.setup_id, "no_cvd_divergence_detected")
            return None

        # --- Compute structural SL/TP ---
        if direction == "long":
            # SL: below the most recent price-action low in window_b + ATR buffer.
            div_extreme = float(min(low_window_b))
            stop = div_extreme - atr * sl_buffer_atr
            risk = price - stop
            if risk <= 0:
                _reject(
                    prepared,
                    self.setup_id,
                    "risk_non_positive_long",
                    stop=stop,
                    price=price,
                )
                return None
            # TP1: first leg retrace target from the prior divergence segment on close prices.
            tp1 = float(max(window_a))
            # TP2: prior structural level (1h swing high)
            w1h = prepared.work_1h
            tp2 = None
            if w1h.height > 5:
                sh_mask, sl_mask = _swing_points(w1h, n=3, include_unconfirmed_tail=True)
                sh_prices = w1h.filter(sh_mask)["high"]
                tp2_cands = sh_prices.filter(sh_prices > price)
                tp2 = float(tp2_cands[0]) if tp2_cands.len() > 0 else None
        else:
            # SL: above the most recent price-action high in window_b + ATR buffer.
            div_extreme = float(max(high_window_b))
            stop = div_extreme + atr * sl_buffer_atr
            risk = stop - price
            if risk <= 0:
                _reject(
                    prepared,
                    self.setup_id,
                    "risk_non_positive_short",
                    stop=stop,
                    price=price,
                )
                return None
            # TP1: first leg retrace target from the prior divergence segment on close prices.
            tp1 = float(min(window_a))
            # TP2: prior structural level (1h swing low)
            w1h = prepared.work_1h
            tp2 = None
            if w1h.height > 5:
                _, sl_mask = _swing_points(w1h, n=3, include_unconfirmed_tail=True)
                sl_prices = w1h.filter(sl_mask)["low"]
                tp2_cands = sl_prices.filter(sl_prices < price)
                tp2 = float(tp2_cands[-1]) if tp2_cands.len() > 0 else None

        # Validate: TP1 must clear the configured R threshold.
        if tp1 is None or abs(tp1 - price) < risk * min_rr:
            _reject(
                prepared,
                self.setup_id,
                "tp1_too_close_or_missing",
                tp1=tp1,
                risk=risk,
                min_rr=min_rr,
                price=price,
            )
            return None  # Reject this CVD divergence setup
        if tp2 is None or abs(tp2 - price) <= abs(tp1 - price):
            tp2 = (
                price + risk * max(2.0, min_rr + 0.35)
                if direction == "long"
                else price - risk * max(2.0, min_rr + 0.35)
            )

        vol_ratio = float(w.item(-1, "volume_ratio20") or 1.0)
        rsi = float(w.item(-1, "rsi14") or 50.0)
        score = _compute_dynamic_score(
            direction=direction,
            base_score=base_score,
            vol_ratio=vol_ratio,
            rsi=rsi,
        )

        reasons = [
            f"CVD divergence {direction}",
            f"delta_a={delta_mean_a:.3f} delta_b={delta_mean_b:.3f} shift={delta_shift:.3f}",
            f"bias_1h={bias_1h}",
        ]

        return _build_signal(
            prepared=prepared,
            setup_id=self.setup_id,
            direction=direction,
            score=score,
            timeframe="15m+1h",
            reasons=reasons,
            strategy_family=self.family,
            stop=stop,
            tp1=tp1,
            tp2=tp2,
            price_anchor=price,
            atr=atr,
        )
