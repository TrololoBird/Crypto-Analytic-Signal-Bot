"""ema_bounce — simplified trend continuation setup.

# WINDSURF_REVIEW: unified + vectorized + 1H context + graded
"""

from __future__ import annotations

from ..domain.config import BotSettings
from ..domain.schemas import PreparedSymbol, Signal
from ..setup_base import BaseSetup
from ..setups import _build_signal, _compute_dynamic_score, _reject
from ..setups.utils import (
    build_structural_targets,
    validate_rr_or_penalty,
    get_dynamic_params,
)


class EmaBounceSetup(BaseSetup):
    setup_id = "ema_bounce"
    family = "continuation"
    confirmation_profile = "trend_follow"
    required_context = ("futures_flow",)

    def get_optimizable_params(self, settings: BotSettings | None = None) -> dict[str, float]:
        """Tunable parameters for self-learner optimization.

        If settings provided, reads from config [bot.filters.setups].
        Otherwise returns hardcoded defaults.
        """
        defaults = {
            "base_score": 0.55,
            "min_adx_1h": 15.0,
            "vol_ratio_threshold": 1.0,
            "bias_mismatch_penalty": 0.75,
            "tp_too_close_penalty": 0.75,
            "min_rr": 1.9,
            "sl_buffer_atr": 0.5,
            "ema_touch_tolerance_pct": 0.005,
            "ema_touch_tolerance": 0.005,
        }

        if settings is not None:
            # Try to get from config
            filters = getattr(settings, "filters", None)
            if filters:
                setups_config = getattr(filters, "setups", {})
                if isinstance(setups_config, dict) and self.setup_id in setups_config:
                    config_params = setups_config.get(self.setup_id, {})
                    # Merge config with defaults (config takes precedence)
                    return {**defaults, **config_params}

        return defaults

    def detect(self, prepared: PreparedSymbol, settings: BotSettings) -> Signal | None:
        setup_id = self.setup_id
        dynamic_params = get_dynamic_params(prepared, setup_id)
        defaults = self.get_optimizable_params(settings)

        ema_touch_tolerance_pct = float(
            dynamic_params.get(
                "ema_touch_tolerance_pct",
                dynamic_params.get(
                    "ema_touch_tolerance",
                    defaults.get(
                        "ema_touch_tolerance_pct",
                        defaults.get("ema_touch_tolerance", 0.008),
                    ),
                ),
            )
        )
        bounce_threshold_pct = dynamic_params.get("bounce_threshold_pct", 0.005)
        min_adx = dynamic_params.get(
            "min_adx",
            dynamic_params.get("min_adx_1h", defaults.get("min_adx", defaults["min_adx_1h"])),
        )
        sl_buffer_atr = float(
            dynamic_params.get("sl_buffer_atr", defaults.get("sl_buffer_atr", 1.5))
        )

        work_1h = prepared.work_1h
        work_15m = prepared.work_15m
        context_timeframe = "15m+1h"
        if work_1h.height < 3 or work_15m.height < 5:
            _reject(
                prepared,
                setup_id,
                "insufficient_context_bars",
                bars_1h=work_1h.height,
                bars_15m=work_15m.height,
                required_1h=3,
                required_15m=5,
            )
            return None
        required_1h = {"ema20", "ema50", "close", "adx14"}
        required_15m = {
            "open",
            "high",
            "low",
            "close",
            "atr14",
            "ema20",
            "ema50",
            "volume_ratio20",
            "rsi14",
            "close_position",
        }
        missing_columns = sorted(
            required_1h.difference(work_1h.columns) | required_15m.difference(work_15m.columns)
        )
        if missing_columns:
            _reject(
                prepared,
                setup_id,
                "missing_columns",
                missing_fields=missing_columns,
                context_timeframe=context_timeframe,
            )
            return None

        atr = float(work_15m.item(-1, "atr14") or 0.0)
        ema20_1h = float(work_1h.item(-1, "ema20") or 0.0)
        ema50_1h = float(work_1h.item(-1, "ema50") or 0.0)
        close_1h = float(work_1h.item(-1, "close"))
        ema20 = float(work_15m.item(-1, "ema20") or 0.0)
        ema50 = float(work_15m.item(-1, "ema50") or 0.0)
        open_ = float(work_15m.item(-1, "open") or 0.0)
        high = float(work_15m.item(-1, "high") or 0.0)
        low = float(work_15m.item(-1, "low") or 0.0)
        close = float(work_15m.item(-1, "close"))
        prev_close = float(work_15m.item(-2, "close"))
        close_position = float(work_15m.item(-1, "close_position") or 0.5)

        if min(atr, ema20_1h, ema50_1h, ema20, ema50, open_, high, low, close) <= 0.0:
            _reject(
                prepared,
                setup_id,
                "invalid_indicator_state",
                atr=atr,
                ema20_1h=ema20_1h,
                ema50_1h=ema50_1h,
                ema20_15m=ema20,
                ema50_15m=ema50,
            )
            return None

        reasons: list[str] = []

        bias_1h = getattr(prepared, "bias_1h", prepared.bias_4h)
        if bias_1h not in {"uptrend", "downtrend"}:
            if close_1h > ema50_1h and ema20_1h > ema50_1h:
                bias_1h = "uptrend"
            elif close_1h < ema50_1h and ema20_1h < ema50_1h:
                bias_1h = "downtrend"

        signal_direction: str | None = None
        recent = work_15m.tail(5)
        if bias_1h == "uptrend":
            recent_low = float(recent["low"].min())
            touch_ema = recent_low <= ema20 * (
                1.0 + float(ema_touch_tolerance_pct)
            ) or recent_low <= ema50 * (1.0 + float(ema_touch_tolerance_pct) * 2.0)
            bounce = (
                close > open_
                and close >= prev_close * (1.0 + float(bounce_threshold_pct))
                and close >= ema20 * (1.0 - float(ema_touch_tolerance_pct))
                and close_position >= 0.55
            )
            if touch_ema and bounce:
                signal_direction = "long"
                reasons = [
                    "ema_bounce_long",
                    f"context_tf={context_timeframe}",
                    f"ema20_1h={ema20_1h:.4f}",
                    f"ema50_1h={ema50_1h:.4f}",
                    f"ema20_15m={ema20:.4f}",
                ]
        elif bias_1h == "downtrend":
            recent_high = float(recent["high"].max())
            touch_ema = recent_high >= ema20 * (
                1.0 - float(ema_touch_tolerance_pct)
            ) or recent_high >= ema50 * (1.0 - float(ema_touch_tolerance_pct) * 2.0)
            bounce = (
                close < open_
                and close <= prev_close * (1.0 - float(bounce_threshold_pct))
                and close <= ema20 * (1.0 + float(ema_touch_tolerance_pct))
                and close_position <= 0.45
            )
            if touch_ema and bounce:
                signal_direction = "short"
                reasons = [
                    "ema_bounce_short",
                    f"context_tf={context_timeframe}",
                    f"ema20_1h={ema20_1h:.4f}",
                    f"ema50_1h={ema50_1h:.4f}",
                    f"ema20_15m={ema20:.4f}",
                ]

        if signal_direction is None:
            _reject(prepared, setup_id, "no_bounce_pattern", bias_1h=bias_1h)
            return None

        vol_ratio = float(work_15m.item(-1, "volume_ratio20") or 1.0)
        rsi = float(work_15m.item(-1, "rsi14") or 50.0)
        adx_1h = float(work_1h.item(-1, "adx14") or 0.0)
        if adx_1h > 0.0 and adx_1h < float(min_adx):
            _reject(prepared, setup_id, "adx_too_low", adx_1h=adx_1h, min_adx=min_adx)
            return None
        price_anchor = close

        # --- Compute structural SL/TP via unified utility ---
        from ..features import _swing_points as _sp

        sh_mask, sl_mask = _sp(work_1h, n=3, include_unconfirmed_tail=True)

        # Determine bounce EMA for SL basis
        if signal_direction == "long":
            bounce_ema = min(ema20, ema50) if prev_close <= ema50 * 1.01 else ema20
        else:
            bounce_ema = max(ema20, ema50) if prev_close >= ema50 * 0.99 else ema20

        stop, tp1, tp2 = build_structural_targets(
            direction=signal_direction,
            price_anchor=price_anchor,
            stop_basis=bounce_ema,
            atr=atr,
            work_1h=work_1h,
            min_rr=dynamic_params.get("min_rr", defaults["min_rr"]),
            sl_buffer_atr=sl_buffer_atr,
            sh_mask=sh_mask,
            sl_mask=sl_mask,
        )
        if signal_direction == "long" and stop >= price_anchor:
            stop = price_anchor - atr * 0.5
            reasons.append("stop_reanchored_below_entry")
        if signal_direction == "short" and stop <= price_anchor:
            stop = price_anchor + atr * 0.5
            reasons.append("stop_reanchored_above_entry")

        # Graded RR validation (penalty instead of reject)
        min_rr = dynamic_params.get("min_rr", defaults["min_rr"])
        is_valid_rr, _ = validate_rr_or_penalty(price_anchor, stop, tp1, min_rr)

        base_score = dynamic_params.get("base_score", defaults["base_score"])
        score = _compute_dynamic_score(
            direction=signal_direction,
            base_score=base_score,
            vol_ratio=vol_ratio,
            rsi=rsi,
            structure_clarity=0.3,
        )

        # Apply graded penalty for RR issues (not reject)
        if not is_valid_rr and tp1 is not None:
            score *= dynamic_params.get("tp_too_close_penalty", defaults["tp_too_close_penalty"])
            reasons.append("tp_too_close_penalty")

        if tp1 is None:
            risk = abs(price_anchor - stop)
            if risk <= 0.0:
                _reject(
                    prepared,
                    setup_id,
                    "tp1_missing_invalid_risk",
                    direction=signal_direction,
                    price_anchor=price_anchor,
                )
                return None
            rr_multiplier = float(min_rr)
            if signal_direction == "long":
                tp1 = price_anchor + risk * rr_multiplier
            else:
                tp1 = price_anchor - risk * rr_multiplier
            reasons.append("tp1_atr_fallback")

        if tp2 is None or abs(tp2 - price_anchor) <= abs(tp1 - price_anchor):
            risk = abs(price_anchor - stop)
            tp2 = (
                price_anchor + risk * max(2.0, float(min_rr) + 0.35)
                if signal_direction == "long"
                else price_anchor - risk * max(2.0, float(min_rr) + 0.35)
            )

        return _build_signal(
            prepared=prepared,
            setup_id=setup_id,
            direction=signal_direction,
            score=score,
            timeframe=context_timeframe,
            reasons=reasons,
            strategy_family=self.family,
            stop=stop,
            tp1=tp1,
            tp2=tp2,
            price_anchor=close,
            atr=atr,
        )
