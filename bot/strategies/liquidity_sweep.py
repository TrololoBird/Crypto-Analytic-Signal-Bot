"""Liquidity Sweep setup detector.

Detects sweep of equal highs/lows (liquidity pools) on work_1h.
Equal levels = 2+ peaks within 0.15% of each other in last 30 bars.
Sweep = recent bar's wick breaks the level but closes back inside.

# WINDSURF_REVIEW: unified + vectorized + 1H context + graded
"""

from __future__ import annotations

import logging
import math


from ..domain.config import BotSettings
from ..domain.schemas import PreparedSymbol, Signal
from ..setup_base import BaseSetup
from ..setups import _build_signal, _compute_dynamic_score, _reject
from ..setups.smc import latest_liquidity_sweep
from ..setups.utils import get_dynamic_params

LOG = logging.getLogger("bot.strategies.liquidity_sweep")

_SCAN_BARS = 30
_EQUAL_TOL = 0.0015  # 0.15%


def _as_float(value: object, default: float = 0.0) -> float:
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    return default


class LiquiditySweepSetup(BaseSetup):
    setup_id = "liquidity_sweep"
    family = "reversal"
    confirmation_profile = "countertrend_exhaustion"
    required_context = ("futures_flow",)

    def get_optimizable_params(self, settings: BotSettings | None = None) -> dict[str, float]:
        """Tunable parameters for self-learner optimization."""
        defaults = {
            "base_score": 0.50,
            "equal_level_tol": 0.0015,
            "threshold_tol": 0.0015,  # Backward-compatible alias from existing config files.
            "min_level_hits": 2,
            "sweep_atr_mult": 0.30,
            "reclaim_threshold": 0.30,
            "sl_buffer_atr": 0.50,
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
        dynamic_params = get_dynamic_params(prepared, self.setup_id)
        defaults = self.get_optimizable_params(settings)
        equal_level_tol = float(
            dynamic_params.get(
                "equal_level_tol",
                dynamic_params.get("threshold_tol", defaults["equal_level_tol"]),
            )
        )
        min_level_hits = max(
            2, int(dynamic_params.get("min_level_hits", defaults["min_level_hits"]))
        )
        sweep_atr_mult = float(dynamic_params.get("sweep_atr_mult", defaults["sweep_atr_mult"]))
        reclaim_threshold = float(
            dynamic_params.get("reclaim_threshold", defaults["reclaim_threshold"])
        )
        sl_buffer_atr = float(dynamic_params.get("sl_buffer_atr", defaults["sl_buffer_atr"]))
        min_rr = float(dynamic_params.get("min_rr", defaults["min_rr"]))
        base_score = float(dynamic_params.get("base_score", defaults["base_score"]))

        try:
            return self._detect(
                prepared,
                equal_level_tol=equal_level_tol,
                min_level_hits=min_level_hits,
                sweep_atr_mult=sweep_atr_mult,
                reclaim_threshold=reclaim_threshold,
                sl_buffer_atr=sl_buffer_atr,
                min_rr=min_rr,
                base_score=base_score,
            )
        except Exception as exc:
            LOG.exception("%s liquidity_sweep: unexpected error", prepared.symbol)
            _reject(
                prepared,
                self.setup_id,
                "runtime.unexpected_exception",
                stage="runtime",
                exception_type=type(exc).__name__,
            )
            return None

    def _detect(
        self,
        prepared: PreparedSymbol,
        *,
        equal_level_tol: float,
        min_level_hits: int,
        sweep_atr_mult: float,
        reclaim_threshold: float,
        sl_buffer_atr: float,
        min_rr: float,
        base_score: float,
    ) -> Signal | None:
        setup_id = self.setup_id

        w = prepared.work_1h
        if w.height < 10:
            _reject(prepared, setup_id, "insufficient_1h_bars", bars=w.height)
            return None

        atr = float(w.item(-1, "atr14") or 0.0)
        if atr <= 0 or math.isnan(atr):
            _reject(prepared, setup_id, "atr_invalid", atr=atr)
            return None

        price = prepared.mark_price or prepared.universe.last_price
        if not price or price <= 0:
            _reject(prepared, setup_id, "price_missing")
            return None

        scan = w.tail(_SCAN_BARS) if w.height >= _SCAN_BARS else w
        highs = scan["high"].to_numpy()
        lows = scan["low"].to_numpy()
        closes = scan["close"].to_numpy()
        n = len(scan)

        if n < 3:
            _reject(prepared, setup_id, "scan_window_insufficient", bars=n)
            return None

        zone = latest_liquidity_sweep(
            scan,
            swing_length=max(2, min_level_hits + 1),
            range_percent=equal_level_tol,
        )
        if (
            zone is None
            or zone.sweep_index is None
            or zone.sweep_index < scan.height - 2
            or zone.sweep_index > scan.height - 1
            or zone.state == "invalidated"
        ):
            _reject(prepared, setup_id, "no_liquidity_sweep_detected")
            return None

        sweep_index = int(zone.sweep_index)
        sweep_bar_h = float(highs[sweep_index])
        sweep_bar_l = float(lows[sweep_index])
        sweep_bar_c = float(closes[sweep_index])
        confirmation_close = float(closes[-1])

        if zone.direction == "short":
            eq_high_level = zone.level or zone.midpoint
            if (
                sweep_bar_h <= eq_high_level
                or sweep_bar_c >= eq_high_level
                or confirmation_close >= eq_high_level
            ):
                _reject(
                    prepared,
                    setup_id,
                    "short_reclaim_not_confirmed",
                    level=eq_high_level,
                )
                return None
            if abs(price - confirmation_close) > sweep_atr_mult * atr:
                _reject(
                    prepared,
                    setup_id,
                    "short_reclaim_too_far",
                    price=price,
                    close=confirmation_close,
                )
                return None

            stop = sweep_bar_h + sl_buffer_atr * atr
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

            rr_tp1 = price - risk * min_rr
            from ..features import _swing_points as _sp

            _, sl_mask = _sp(w, n=3, include_unconfirmed_tail=True)
            sl_prices = w.filter(sl_mask)["low"]
            tp2_candidates = sl_prices.filter(sl_prices < price)
            structural_tp1 = _as_float(tp2_candidates[-1]) if tp2_candidates.len() > 0 else None
            tp1 = (
                structural_tp1
                if structural_tp1 is not None and abs(structural_tp1 - price) >= risk * min_rr
                else rr_tp1
            )
            if tp1 >= price or abs(tp1 - price) < risk * min_rr:
                _reject(
                    prepared,
                    setup_id,
                    "tp1_too_close_or_missing",
                    tp1=tp1,
                    risk=risk,
                    price=price,
                )
                return None
            tp2 = _as_float(tp2_candidates[-1]) if tp2_candidates.len() > 0 else None
            if tp2 is None or abs(tp2 - price) <= abs(tp1 - price):
                tp2 = tp1

            vol_ratio = _as_float(w.item(-1, "volume_ratio20"), 1.0)
            rsi = _as_float(w.item(-1, "rsi14"), 50.0)
            score = _compute_dynamic_score(
                direction="short",
                base_score=base_score,
                vol_ratio=vol_ratio,
                rsi=rsi,
            )
            reasons = [
                f"Liquidity sweep short: eq_high={eq_high_level:.4f} state={zone.state}",
                (
                    f"wick={sweep_bar_h:.4f} close={sweep_bar_c:.4f} "
                    f"confirm={confirmation_close:.4f}"
                ),
            ]
            return _build_signal(
                prepared=prepared,
                setup_id=self.setup_id,
                direction="short",
                score=score,
                timeframe="1h",
                reasons=reasons,
                strategy_family=self.family,
                stop=stop,
                tp1=tp1,
                tp2=tp2,
                price_anchor=confirmation_close,
                atr=atr,
            )

        eq_low_level = zone.level or zone.midpoint
        if (
            sweep_bar_l >= eq_low_level
            or sweep_bar_c <= eq_low_level
            or confirmation_close <= eq_low_level
        ):
            _reject(prepared, setup_id, "long_reclaim_not_confirmed", level=eq_low_level)
            return None
        if abs(price - confirmation_close) > reclaim_threshold * atr:
            _reject(
                prepared,
                setup_id,
                "long_reclaim_too_far",
                price=price,
                close=confirmation_close,
            )
            return None

        stop = sweep_bar_l - sl_buffer_atr * atr
        risk = price - stop
        if risk <= 0:
            _reject(prepared, setup_id, "risk_non_positive_long", stop=stop, price=price)
            return None

        rr_tp1 = price + risk * min_rr
        from ..features import _swing_points as _sp

        sh_mask, _ = _sp(w, n=3, include_unconfirmed_tail=True)
        sh_prices = w.filter(sh_mask)["high"]
        tp2_candidates = sh_prices.filter(sh_prices > price)
        structural_tp1 = _as_float(tp2_candidates[-1]) if tp2_candidates.len() > 0 else None
        tp1 = (
            structural_tp1
            if structural_tp1 is not None and abs(structural_tp1 - price) >= risk * min_rr
            else rr_tp1
        )
        if tp1 <= price or abs(tp1 - price) < risk * min_rr:
            _reject(
                prepared,
                setup_id,
                "tp1_too_close_or_missing",
                tp1=tp1,
                risk=risk,
                price=price,
            )
            return None
        tp2 = _as_float(tp2_candidates[-1]) if tp2_candidates.len() > 0 else None
        if tp2 is None or abs(tp2 - price) <= abs(tp1 - price):
            tp2 = tp1

        vol_ratio = _as_float(w.item(-1, "volume_ratio20"), 1.0)
        rsi = _as_float(w.item(-1, "rsi14"), 50.0)
        score = _compute_dynamic_score(
            direction="long",
            base_score=base_score,
            vol_ratio=vol_ratio,
            rsi=rsi,
        )
        reasons = [
            f"Liquidity sweep long: eq_low={eq_low_level:.4f} state={zone.state}",
            (f"wick={sweep_bar_l:.4f} close={sweep_bar_c:.4f} confirm={confirmation_close:.4f}"),
        ]
        return _build_signal(
            prepared=prepared,
            setup_id=self.setup_id,
            direction="long",
            score=score,
            timeframe="1h",
            reasons=reasons,
            strategy_family=self.family,
            stop=stop,
            tp1=tp1,
            tp2=tp2,
            price_anchor=confirmation_close,
            atr=atr,
        )
