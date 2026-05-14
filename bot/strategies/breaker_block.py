"""Breaker Block setup detector.

An Order Block that has been broken and now acts as resistance/support
from the opposite side. Price returning to test the former OB zone.

# WINDSURF_REVIEW: unified + vectorized + 1H context + graded
"""

from __future__ import annotations

import logging
import math

from ..setup_base import BaseSetup
from ..domain.config import BotSettings
from ..domain.schemas import PreparedSymbol, Signal
from ..setups import _build_signal, _compute_dynamic_score, _reject
from ..setups.smc import latest_breaker_block
from ..setups.utils import get_dynamic_params

LOG = logging.getLogger("bot.strategies.breaker_block")

_SCAN_BARS = 40


def _as_float(value: object, default: float = 0.0) -> float:
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        numeric = float(value)
        return numeric if math.isfinite(numeric) else default
    return default


def _last(frame: object, column: str, default: float = 0.0) -> float:
    if not hasattr(frame, "is_empty") or frame.is_empty() or column not in frame.columns:
        return default
    return _as_float(frame.item(-1, column), default)


class BreakerBlockSetup(BaseSetup):
    setup_id = "breaker_block"
    family = "breakout"
    confirmation_profile = "breakout_acceptance"
    required_context = ("futures_flow",)

    def get_optimizable_params(self, settings: BotSettings | None = None) -> dict[str, float]:
        """Tunable parameters for self-learner optimization."""
        defaults = {
            "base_score": 0.52,
            "scan_bars": 40,
            "mitigation_threshold": 0.20,
            "sl_buffer_atr": 0.20,
            "min_atr": 0.0001,
            "min_volume_ratio": 0.90,
            "min_acceptance_close_position_long": 0.50,
            "max_acceptance_close_position_short": 0.50,
            "bias_mismatch_penalty": 0.75,
            "min_rr": 1.5,
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
            LOG.exception("%s breaker_block: unexpected error", prepared.symbol)
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

        scan_bars = max(15, int(dynamic_params.get("scan_bars", defaults["scan_bars"])))
        mitigation_threshold = float(
            dynamic_params.get("mitigation_threshold", defaults["mitigation_threshold"])
        )
        sl_buffer_atr = float(dynamic_params.get("sl_buffer_atr", defaults["sl_buffer_atr"]))
        min_rr = float(dynamic_params.get("min_rr", defaults["min_rr"]))
        base_score = float(dynamic_params.get("base_score", defaults["base_score"]))
        min_volume_ratio = float(
            dynamic_params.get("min_volume_ratio", defaults["min_volume_ratio"])
        )

        w1h = prepared.work_1h
        if w1h.height < 15:
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

        scan = w1h.tail(scan_bars) if w1h.height >= scan_bars else w1h
        zone = latest_breaker_block(
            scan,
            swing_length=3,
            current_price=price,
            retest_buffer=mitigation_threshold * atr,
        )
        if zone is None:
            _reject(prepared, setup_id, "no_breaker_block_detected")
            return None

        direction = zone.direction
        bb_low = zone.bottom
        bb_high = zone.top

        vol_ratio_15m = _last(prepared.work_15m, "volume_ratio20", 1.0)
        if vol_ratio_15m < min_volume_ratio:
            _reject(
                prepared,
                setup_id,
                "volume_too_low",
                volume_ratio=vol_ratio_15m,
                min_volume_ratio=min_volume_ratio,
            )
            return None

        close_position = _last(prepared.work_15m, "close_position", 0.5)
        if direction == "long" and close_position < float(
            dynamic_params.get(
                "min_acceptance_close_position_long",
                defaults["min_acceptance_close_position_long"],
            )
        ):
            _reject(
                prepared,
                setup_id,
                "retest_acceptance_missing",
                direction=direction,
                close_position=close_position,
            )
            return None
        if direction == "short" and close_position > float(
            dynamic_params.get(
                "max_acceptance_close_position_short",
                defaults["max_acceptance_close_position_short"],
            )
        ):
            _reject(
                prepared,
                setup_id,
                "retest_acceptance_missing",
                direction=direction,
                close_position=close_position,
            )
            return None

        # --- Compute structural SL/TP ---
        from ..features import _swing_points as _sp

        if direction == "long":
            # SL: beyond breaker block level + sl_buffer_atr×ATR.
            stop = bb_low - sl_buffer_atr * atr
            risk = price - stop
            if risk <= 0:
                _reject(prepared, setup_id, "risk_non_positive_long", stop=stop, price=price)
                return None
            # TP1: next 1h swing high (liquidity target / imbalance fill)
            sh_mask, _ = _sp(w1h, n=3, include_unconfirmed_tail=True)
            sh_prices = w1h.filter(sh_mask)["high"]
            tp1_candidates = sh_prices.filter(sh_prices > price)
            tp1 = float(tp1_candidates[0]) if tp1_candidates.len() > 0 else None
            # TP2: 4h structural resistance
            w4h = prepared.work_4h
            tp2 = None
            if w4h is not None and w4h.height > 5:
                sh4_mask, _ = _sp(w4h, n=2)
                sh4_prices = w4h.filter(sh4_mask)["high"]
                tp2_cands = sh4_prices.filter(sh4_prices > price)
                tp2 = float(tp2_cands[0]) if tp2_cands.len() > 0 else None
        else:
            # SL: beyond breaker block level + sl_buffer_atr×ATR.
            stop = bb_high + sl_buffer_atr * atr
            risk = stop - price
            if risk <= 0:
                _reject(
                    prepared,
                    setup_id,
                    "risk_non_positive_short",
                    stop=stop,
                    price=price,
                )
                return None
            # TP1: next 1h swing low (liquidity target)
            _, sl_mask = _sp(w1h, n=3, include_unconfirmed_tail=True)
            sl_prices = w1h.filter(sl_mask)["low"]
            tp1_candidates = sl_prices.filter(sl_prices < price)
            tp1 = float(tp1_candidates[-1]) if tp1_candidates.len() > 0 else None
            # TP2: 4h structural support
            w4h = prepared.work_4h
            tp2 = None
            if w4h is not None and w4h.height > 5:
                _, sl4_mask = _sp(w4h, n=2)
                sl4_prices = w4h.filter(sl4_mask)["low"]
                tp2_cands = sl4_prices.filter(sl4_prices < price)
                tp2 = float(tp2_cands[-1]) if tp2_cands.len() > 0 else None

        fallback_note = None
        if tp1 is None or abs(tp1 - price) < risk * min_rr:
            tp1 = price + risk * min_rr if direction == "long" else price - risk * min_rr
            fallback_note = f"tp1_rr_fallback_{min_rr:.2f}"
        if tp2 is None:
            tp2 = tp1  # Use TP1 as TP2 if no extended target found

        vol_ratio = float(w1h.item(-1, "volume_ratio20") or 1.0)
        rsi = float(w1h.item(-1, "rsi14") or 50.0)
        score = _compute_dynamic_score(
            direction=direction,
            base_score=base_score,
            vol_ratio=vol_ratio,
            rsi=rsi,
        )

        reasons = [
            f"Breaker block {direction}: zone [{bb_low:.4f}-{bb_high:.4f}] state={zone.state}",
            f"price={price:.4f} retesting broken OB from {zone.metadata.get('source_ob_direction')}",
        ]
        if fallback_note:
            reasons.append(fallback_note)

        return _build_signal(
            prepared=prepared,
            setup_id=self.setup_id,
            direction=direction,
            score=score,
            timeframe="1h",
            reasons=reasons,
            strategy_family=self.family,
            stop=stop,
            tp1=tp1,
            tp2=tp2,
            price_anchor=price,
            atr=atr,
        )
