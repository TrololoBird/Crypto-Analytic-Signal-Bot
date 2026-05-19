"""Hidden Divergence setup detector.

Hidden Bullish: price higher low + RSI lower low (continuation long)
Hidden Bearish: price lower high + RSI higher high (continuation short)

Uses swing points on work_1h for structure detection.
Requires 1H trend alignment (continuation signal).

# WINDSURF_REVIEW: unified + vectorized + 1H context + graded
"""

from __future__ import annotations

import logging
import math

from ..setup_base import BaseSetup
from ..domain.config import BotSettings
from ..domain.schemas import PreparedSymbol, Signal
from ..setups import _build_signal, _compute_dynamic_score, _reject
from ..features import _swing_points
from ..setups.utils import get_dynamic_params
from .common import as_float

LOG = logging.getLogger("bot.strategies.hidden_divergence")


def _finite_series_values(values: object) -> list[float]:
    try:
        raw_values = values.to_list()
    except AttributeError:
        raw_values = list(values or [])
    out: list[float] = []
    for value in raw_values:
        numeric = as_float(value, default=math.nan)
        if math.isfinite(numeric):
            out.append(numeric)
    return out


def _find_recent_hidden_divergence_pair(
    prices: object,
    oscillators: object,
    *,
    direction: str,
    min_oscillator_separation: float,
    max_pair_gap: int,
) -> tuple[float, float] | None:
    price_values = _finite_series_values(prices)
    oscillator_values = _finite_series_values(oscillators)
    count = min(len(price_values), len(oscillator_values))
    if count < 2:
        return None
    price_values = price_values[-count:]
    oscillator_values = oscillator_values[-count:]
    pair_gap = max(1, int(max_pair_gap))

    for current_idx in range(count - 1, 0, -1):
        first_idx = max(0, current_idx - pair_gap)
        for previous_idx in range(current_idx - 1, first_idx - 1, -1):
            previous_price = price_values[previous_idx]
            current_price = price_values[current_idx]
            previous_oscillator = oscillator_values[previous_idx]
            current_oscillator = oscillator_values[current_idx]
            if direction == "long":
                oscillator_gap = previous_oscillator - current_oscillator
                if current_price > previous_price and oscillator_gap >= min_oscillator_separation:
                    return current_price, oscillator_gap
            else:
                oscillator_gap = current_oscillator - previous_oscillator
                if current_price < previous_price and oscillator_gap >= min_oscillator_separation:
                    return current_price, oscillator_gap
    return None


class HiddenDivergenceSetup(BaseSetup):
    setup_id = "hidden_divergence"
    family = "continuation"
    confirmation_profile = "trend_follow"
    required_context = ("futures_flow",)

    def get_optimizable_params(self, settings: BotSettings | None = None) -> dict[str, float]:
        """Tunable parameters for self-learner optimization."""
        defaults = {
            "base_score": 0.50,
            "min_swings": 2.0,
            "bias_mismatch_penalty": 0.75,
            "tp_too_close_penalty": 0.75,
            "min_rr": 1.9,
            "rsi_divergence_lookback": 3.0,
            "rsi_divergence_threshold": 2.0,
            "max_swing_pair_gap": 6.0,
            "min_delta_threshold": 0.0,
            "min_volume_ratio": 0.55,
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
            LOG.exception("%s hidden_divergence: unexpected error", prepared.symbol)
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

        rsi_divergence_lookback = int(
            dynamic_params.get("rsi_divergence_lookback", defaults["rsi_divergence_lookback"])
        )
        rsi_divergence_threshold = float(
            dynamic_params.get("rsi_divergence_threshold", defaults["rsi_divergence_threshold"])
        )
        max_swing_pair_gap = int(
            dynamic_params.get("max_swing_pair_gap", defaults["max_swing_pair_gap"])
        )
        min_delta_threshold = float(
            dynamic_params.get("min_delta_threshold", defaults["min_delta_threshold"])
        )
        min_volume_ratio = float(
            dynamic_params.get("min_volume_ratio", defaults["min_volume_ratio"])
        )
        sl_buffer_atr = float(
            dynamic_params.get("sl_buffer_atr", defaults["sl_buffer_atr"])
        )

        w1h = prepared.work_1h
        if w1h.height < 20:
            _reject(prepared, setup_id, "insufficient_1h_bars", bars=w1h.height)
            return None

        atr = float(w1h.item(-1, "atr14") or 0.0)
        if atr <= 0 or math.isnan(atr):
            _reject(prepared, setup_id, "atr_invalid", atr=atr)
            return None

        price = prepared.mark_price or prepared.universe.last_price
        if not price or price <= 0:
            _reject(prepared, setup_id, "price_missing")
            return None

        w15m = prepared.work_15m
        if w15m.height < 3:
            _reject(prepared, setup_id, "insufficient_15m_bars", bars=w15m.height)
            return None
        vol_ratio_15m = float(w15m.item(-1, "volume_ratio20") or 1.0)
        volume_penalty = False
        if vol_ratio_15m < min_volume_ratio:
            volume_penalty = True

        # 1H context for 15M signals (not 4H - too lagging for <4h trades)
        bias_1h = getattr(prepared, "bias_1h", prepared.bias_4h)
        sh_mask, sl_mask = _swing_points(
            w1h, n=max(2, rsi_divergence_lookback), include_unconfirmed_tail=True
        )
        sh_prices = w1h.filter(sh_mask)["high"]
        sh_rsi = w1h.filter(sh_mask)["rsi14"] if "rsi14" in w1h.columns else None
        sl_prices = w1h.filter(sl_mask)["low"]
        sl_rsi = w1h.filter(sl_mask)["rsi14"] if "rsi14" in w1h.columns else None

        # Use 1H context for 15M signals (not 4H - too lagging for <4h trades)
        bias_1h = getattr(prepared, "bias_1h", prepared.bias_4h)
        direction = None
        stop_price = None
        swing_ref = None
        rsi_separation = 0.0

        # Hidden Bullish: price HL (sl[-1] > sl[-2]) + RSI LL (rsi_sl[-1] < rsi_sl[-2])
        impulse_size = None
        swing_ref = None
        if (
            bias_1h in ("uptrend", "neutral")
            and sl_prices.len() >= 2
            and sl_rsi is not None
            and sl_rsi.len() >= 2
        ):
            match = _find_recent_hidden_divergence_pair(
                sl_prices,
                sl_rsi,
                direction="long",
                min_oscillator_separation=rsi_divergence_threshold,
                max_pair_gap=max_swing_pair_gap,
            )
            if match is not None:
                direction = "long"
                swing_ref, rsi_separation = match
                # Compute last impulse wave size for Fib extensions
                if sh_prices.len() >= 1:
                    impulse_size = abs(float(sh_prices.to_numpy()[-1]) - float(swing_ref))

        # Hidden Bearish: price LH (sh[-1] < sh[-2]) + RSI HH (rsi_sh[-1] > rsi_sh[-2])
        if direction is None and (
            bias_1h in ("downtrend", "neutral")
            and sh_prices.len() >= 2
            and sh_rsi is not None
            and sh_rsi.len() >= 2
        ):
            match = _find_recent_hidden_divergence_pair(
                sh_prices,
                sh_rsi,
                direction="short",
                min_oscillator_separation=rsi_divergence_threshold,
                max_pair_gap=max_swing_pair_gap,
            )
            if match is not None:
                direction = "short"
                swing_ref, rsi_separation = match
                if sl_prices.len() >= 1:
                    impulse_size = abs(float(swing_ref) - float(sl_prices.to_numpy()[-1]))

        if direction is None or swing_ref is None:
            _reject(prepared, setup_id, "no_hidden_divergence_detected")
            return None

        # 4H trend must align for continuation
        if direction == "long" and bias_1h == "downtrend":
            _reject(prepared, setup_id, "context_bias_blocks_long", bias_1h=bias_1h)
            return None
        if direction == "short" and bias_1h == "uptrend":
            _reject(prepared, setup_id, "context_bias_blocks_short", bias_1h=bias_1h)
            return None

        latest_delta_ratio: float | None = None
        delta_shift = 0.0
        if "delta_ratio" in w15m.columns:
            delta_series = w15m["delta_ratio"].drop_nulls()
            if delta_series.len() > 0:
                latest_delta_ratio = float(delta_series[-1])
                delta_shift = latest_delta_ratio - 0.5
        delta_penalty = False
        if (
            direction == "long"
            and latest_delta_ratio is not None
            and delta_shift < min_delta_threshold
        ):
            delta_penalty = True
        if (
            direction == "short"
            and latest_delta_ratio is not None
            and delta_shift > -min_delta_threshold
        ):
            delta_penalty = True

        # --- Compute structural SL/TP ---
        if direction == "long":
            # SL: beyond hidden divergence extreme (swing low) + 0.15×ATR
            stop_price = swing_ref - atr * sl_buffer_atr
            risk = price - stop_price
            if risk <= 0:
                _reject(
                    prepared,
                    setup_id,
                    "risk_non_positive_long",
                    stop=stop_price,
                    price=price,
                )
                return None
            # TP1/TP2: Fibonacci 1.272× and 1.618× extension of last impulse wave
            if impulse_size and impulse_size > 0:
                tp1 = price + impulse_size * 1.272
                tp2 = price + impulse_size * 1.618
            else:
                tp1 = None
                tp2 = None
        else:
            # SL: beyond hidden divergence extreme (swing high) + 0.15×ATR
            stop_price = swing_ref + atr * sl_buffer_atr
            risk = stop_price - price
            if risk <= 0:
                _reject(
                    prepared,
                    setup_id,
                    "risk_non_positive_short",
                    stop=stop_price,
                    price=price,
                )
                return None
            # TP1/TP2: Fibonacci extensions of last impulse wave
            if impulse_size and impulse_size > 0:
                tp1 = price - impulse_size * 1.272
                tp2 = price - impulse_size * 1.618
            else:
                tp1 = None
                tp2 = None

        min_rr = float(dynamic_params.get("min_rr", defaults["min_rr"]))
        if tp1 is None or abs(tp1 - price) < risk * min_rr:
            tp1 = price + risk * min_rr if direction == "long" else price - risk * min_rr
            reasons_note = f"tp1_rr_fallback_{min_rr:.2f}"
        else:
            reasons_note = "tp1_fib_extension"
        if tp2 is None or abs(tp2 - price) <= abs(tp1 - price):
            tp2 = (
                price + risk * max(2.0, min_rr + 0.35)
                if direction == "long"
                else price - risk * max(2.0, min_rr + 0.35)
            )

        rsi = float(w1h.item(-1, "rsi14") or 50.0)
        vol_ratio = float(w1h.item(-1, "volume_ratio20") or 1.0)
        score = _compute_dynamic_score(
            direction=direction,
            base_score=float(dynamic_params.get("base_score", defaults["base_score"])),
            vol_ratio=vol_ratio,
            rsi=rsi,
        )
        if delta_penalty:
            score *= float(dynamic_params.get("delta_mismatch_penalty", 0.88))
        if volume_penalty:
            score *= float(dynamic_params.get("volume_penalty", 0.90))

        reasons = [
            f"Hidden div {direction}: swing_ref={swing_ref:.4f} rsi_sep={rsi_separation:.2f}",
            f"vol_ratio_15m={vol_ratio_15m:.2f} delta_shift={delta_shift:.3f} 1h={bias_1h}",
            reasons_note,
        ]
        if volume_penalty:
            reasons.append("volume_penalty")
        if delta_penalty:
            reasons.append("delta_mismatch_penalty")

        return _build_signal(
            prepared=prepared,
            setup_id=self.setup_id,
            direction=direction,
            score=score,
            timeframe="15m+1h",
            reasons=reasons,
            strategy_family=self.family,
            stop=stop_price,
            tp1=tp1,
            tp2=tp2,
            price_anchor=price,
            atr=atr,
        )
