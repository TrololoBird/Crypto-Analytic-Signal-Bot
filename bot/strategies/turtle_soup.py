"""turtle_soup - canonical strategy detector."""

from __future__ import annotations

import logging
import math
from typing import TYPE_CHECKING, ClassVar

from ..features.prepare import _swing_points as _sp
from ..setups import _build_signal, _compute_dynamic_score, _reject
from ..setups.spec_runtime import SpecDetectorSetup, run_setup_detection
from ..setups.utils import normalize_trade_levels
from ._common import SpecHit, _latest_values, confirmed_pattern_frame, with_spec_columns

if TYPE_CHECKING:
    import polars as pl

    from ..domain.config import BotSettings
    from ..domain.schemas import PreparedSymbol, Signal

LOG = logging.getLogger("bot.strategies.turtle_soup")

__all__ = ["detect_turtle_soup"]


def detect_turtle_soup(frame: pl.DataFrame, *, timeframe: str = "15m") -> SpecHit | None:
    work = with_spec_columns(frame)
    if work.height < 25:
        return None
    row = _latest_values(work)
    atr = row.get("spec_atr14", 0.0)
    if atr <= 0.0:
        return None
    high = row["high"]
    low = row["low"]
    close = row["close"]
    upper = row.get("spec_prev_high20", 0.0)
    lower = row.get("spec_prev_low20", 0.0)
    vol_ratio = row.get("volume_ratio20", 1.0)
    rsi = row.get("rsi14", 50.0)
    if low < lower and close > lower:
        return SpecHit(
            strategy="turtle_soup",
            direction="long",
            entry=lower,
            stop_basis=low,
            atr=atr,
            timeframe=timeframe,
            reasons=(f"donchian_false_break_low={lower:.4f}",),
            vol_ratio=vol_ratio,
            rsi=rsi,
        )
    if high > upper and close < upper:
        return SpecHit(
            strategy="turtle_soup",
            direction="short",
            entry=upper,
            stop_basis=high,
            atr=atr,
            timeframe=timeframe,
            reasons=(f"donchian_false_break_high={upper:.4f}",),
            vol_ratio=vol_ratio,
            rsi=rsi,
        )
    return None


def _as_float(value: object, default: float = 0.0) -> float:
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        numeric = float(value)
        return numeric if math.isfinite(numeric) else default
    return default


def _finite_or_none(value: object) -> float | None:
    if value is None:
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    return numeric if math.isfinite(numeric) else None


def _orderflow_recovered(
    prepared: PreparedSymbol,
    direction: str,
    *,
    delta_ratio: float | None,
    min_delta_long: float,
    max_delta_short: float,
    max_adverse_depth: float,
    max_adverse_micro: float,
) -> tuple[bool, dict[str, float]]:
    depth = _finite_or_none(prepared.depth_imbalance)
    micro = _finite_or_none(prepared.microprice_bias)
    details: dict[str, float] = {}
    if delta_ratio is not None:
        details["delta_ratio"] = delta_ratio
    if depth is not None:
        details["depth_imbalance"] = depth
    if micro is not None:
        details["microprice_bias"] = micro

    if direction == "long":
        if delta_ratio is not None and delta_ratio < min_delta_long:
            return False, details
        if depth is not None and depth <= -max_adverse_depth:
            return False, details
        if micro is not None and micro <= -max_adverse_micro:
            return False, details
    else:
        if delta_ratio is not None and delta_ratio > max_delta_short:
            return False, details
        if depth is not None and depth >= max_adverse_depth:
            return False, details
        if micro is not None and micro >= max_adverse_micro:
            return False, details
    return True, details


def _detect_turtle_soup_extended(
    prepared: PreparedSymbol,
    _settings: BotSettings,
    defaults: dict[str, float],
    effective: dict[str, float],
    setup_id: str,
    family: str,
) -> Signal | None:
    dynamic_params = effective
    roll_bars = max(5, int(dynamic_params.get("roll_bars", defaults["roll_bars"])))
    break_atr_mult = float(dynamic_params.get("break_atr_mult", defaults["break_atr_mult"]))
    sl_buffer_atr = float(dynamic_params.get("sl_buffer_atr", defaults["sl_buffer_atr"]))
    volume_threshold = float(dynamic_params.get("volume_threshold", defaults["volume_threshold"]))
    min_rr = float(dynamic_params.get("min_rr", defaults["min_rr"]))
    base_score = float(dynamic_params.get("base_score", defaults["base_score"]))
    min_recovery_delta_long = float(
        dynamic_params.get("min_recovery_delta_long", defaults["min_recovery_delta_long"])
    )
    max_recovery_delta_short = float(
        dynamic_params.get("max_recovery_delta_short", defaults["max_recovery_delta_short"])
    )
    max_adverse_depth = float(
        dynamic_params.get(
            "max_adverse_depth_imbalance",
            defaults["max_adverse_depth_imbalance"],
        )
    )
    max_adverse_micro = float(
        dynamic_params.get(
            "max_adverse_microprice_bias",
            defaults["max_adverse_microprice_bias"],
        )
    )
    false_breakout_lookback_1h = max(
        1,
        int(
            dynamic_params.get(
                "false_breakout_lookback_1h",
                defaults["false_breakout_lookback_1h"],
            )
        ),
    )
    confirmation_lookback_15m = max(
        1,
        int(
            dynamic_params.get(
                "confirmation_lookback_15m",
                defaults["confirmation_lookback_15m"],
            )
        ),
    )

    w1h = confirmed_pattern_frame(prepared.work_1h)
    if w1h.height < roll_bars + 3:
        _reject(prepared, setup_id, "insufficient_1h_bars", bars=w1h.height)
        return None

    atr = _as_float(w1h.item(-1, "atr14"))
    if atr <= 0 or math.isnan(atr):
        _reject(prepared, setup_id, "atr_invalid", atr=atr)
        return None

    price = prepared.mark_price or prepared.universe.last_price
    if not price or price <= 0:
        _reject(prepared, setup_id, "price_missing")
        return None

    direction = None
    wick_extreme = None
    rolling_high = 0.0
    rolling_low = 0.0
    sweep_close = 0.0
    sweep_lag = 0
    start_idx = max(roll_bars, w1h.height - false_breakout_lookback_1h)
    for idx in range(w1h.height - 1, start_idx - 1, -1):
        roll_window = w1h.slice(idx - roll_bars, roll_bars)
        candidate_high = _as_float(roll_window["high"].max())
        candidate_low = _as_float(roll_window["low"].min())
        bar_high = _as_float(w1h.item(idx, "high"))
        bar_low = _as_float(w1h.item(idx, "low"))
        bar_close = _as_float(w1h.item(idx, "close"))
        if min(candidate_high, candidate_low, bar_high, bar_low, bar_close) <= 0.0:
            continue
        if bar_low < candidate_low - break_atr_mult * atr and bar_close > candidate_low:
            direction = "long"
            wick_extreme = bar_low
            rolling_high = candidate_high
            rolling_low = candidate_low
            sweep_close = bar_close
            sweep_lag = w1h.height - 1 - idx
            break
        if bar_high > candidate_high + break_atr_mult * atr and bar_close < candidate_high:
            direction = "short"
            wick_extreme = bar_high
            rolling_high = candidate_high
            rolling_low = candidate_low
            sweep_close = bar_close
            sweep_lag = w1h.height - 1 - idx
            break

    if direction is None or wick_extreme is None:
        _reject(prepared, setup_id, "no_false_breakout_detected")
        return None

    # Confirm on 15m: first bar closes in direction of reversal with volume > avg
    w15m = confirmed_pattern_frame(prepared.work_15m)
    if w15m.height < 3:
        _reject(prepared, setup_id, "insufficient_15m_bars", bars=w15m.height)
        return None
    confirmation_window = w15m.tail(min(confirmation_lookback_15m, w15m.height))
    vol_ratio_15m = _as_float(confirmation_window["volume_ratio20"].max(), 1.0)
    confirm_close = _as_float(w15m.item(-1, "close"), sweep_close)
    confirmation_ok = False
    for idx in range(confirmation_window.height - 1, -1, -1):
        bar15_open = _as_float(confirmation_window.item(idx, "open"))
        bar15_close = _as_float(confirmation_window.item(idx, "close"))
        bar15_vol = _as_float(confirmation_window.item(idx, "volume_ratio20"), 1.0)
        if direction == "long" and (
            (bar15_close > bar15_open and bar15_vol >= volume_threshold)
            or (bar15_close > rolling_low and confirm_close > rolling_low)
        ):
            confirmation_ok = True
            break
        if direction == "short" and (
            (bar15_close < bar15_open and bar15_vol >= volume_threshold)
            or (bar15_close < rolling_high and confirm_close < rolling_high)
        ):
            confirmation_ok = True
            break

    if direction == "long" and not confirmation_ok:
        _reject(
            prepared,
            setup_id,
            "15m_confirmation_missing_long",
            vol_ratio_15m=vol_ratio_15m,
        )
        return None
    if direction == "short" and not confirmation_ok:
        _reject(
            prepared,
            setup_id,
            "15m_confirmation_missing_short",
            vol_ratio_15m=vol_ratio_15m,
        )
        return None

    delta_ratio = (
        _as_float(w15m.item(-1, "delta_ratio"), 0.5) if "delta_ratio" in w15m.columns else None
    )
    recovered, flow_details = _orderflow_recovered(
        prepared,
        direction,
        delta_ratio=delta_ratio,
        min_delta_long=min_recovery_delta_long,
        max_delta_short=max_recovery_delta_short,
        max_adverse_depth=max_adverse_depth,
        max_adverse_micro=max_adverse_micro,
    )
    if not recovered:
        flow_details["orderflow_conflict"] = 1.0

    # --- Compute structural SL/TP ---
    range_before = max(rolling_high - rolling_low, atr)
    entry_price = rolling_low if direction == "long" else rolling_high

    if direction == "long":
        # SL: beyond false breakout extreme + sl_buffer_atrxATR
        stop = wick_extreme - sl_buffer_atr * atr
        risk = entry_price - stop
        if risk <= 0:
            _reject(
                prepared,
                setup_id,
                "risk_non_positive_long",
                stop=stop,
                close=entry_price,
            )
            return None
        # TP1/TP2 must be above entry for long to satisfy _build_signal contract.
        tp1 = max(rolling_high, entry_price + risk * min_rr)
        sh_mask, _ = _sp(w1h, n=3, include_unconfirmed_tail=True)
        sh_prices = w1h.filter(sh_mask)["high"]
        tp2_candidates = sh_prices.filter(sh_prices > tp1)
        tp2_series = tp2_candidates.drop_nulls()
        tp2 = (
            _as_float(tp2_series[0])
            if tp2_series.len() > 0
            else max(tp1 + range_before, entry_price + risk * (min_rr + 0.5))
        )
    else:
        # SL: beyond false breakout extreme + sl_buffer_atrxATR
        stop = wick_extreme + sl_buffer_atr * atr
        risk = stop - entry_price
        if risk <= 0:
            _reject(
                prepared,
                setup_id,
                "risk_non_positive_short",
                stop=stop,
                close=entry_price,
            )
            return None
        # TP1/TP2 must be below entry for short to satisfy _build_signal contract.
        tp1 = min(rolling_low, entry_price - risk * min_rr)
        _, sl_mask = _sp(w1h, n=3, include_unconfirmed_tail=True)
        sl_prices = w1h.filter(sl_mask)["low"]
        tp2_candidates = sl_prices.filter(sl_prices < tp1)
        tp2 = (
            _as_float(tp2_candidates[-1])
            if tp2_candidates.len() > 0
            else min(tp1 - range_before, entry_price - risk * (min_rr + 0.5))
        )

    # Validate: TP1 must be at least configured risk distance, else reject.
    if tp1 is None or abs(tp1 - entry_price) < risk * min_rr:
        _reject(prepared, setup_id, "tp1_too_close_or_missing", tp1=tp1, risk=risk)
        return None  # Reject this turtle soup setup
    if tp2 is None:
        tp2 = (
            entry_price + risk * max(2.0, min_rr + 0.35)
            if direction == "long"
            else entry_price - risk * max(2.0, min_rr + 0.35)
        )
    if direction == "long" and (tp1 <= entry_price or tp2 <= entry_price):
        _reject(
            prepared,
            setup_id,
            "tp_direction_mismatch_long",
            tp1=tp1,
            tp2=tp2,
            price_anchor=entry_price,
        )
        return None
    if direction == "short" and (tp1 >= entry_price or tp2 >= entry_price):
        _reject(
            prepared,
            setup_id,
            "tp_direction_mismatch_short",
            tp1=tp1,
            tp2=tp2,
            price_anchor=entry_price,
        )
        return None
    normalized_levels = normalize_trade_levels(
        direction=direction,
        price_anchor=entry_price,
        stop=stop,
        tp1=tp1,
        tp2=tp2,
    )
    if normalized_levels is None:
        _reject(
            prepared,
            setup_id,
            "invalid_trade_levels",
            direction=direction,
            stop=stop,
            tp1=tp1,
            tp2=tp2,
            price_anchor=entry_price,
        )
        return None
    stop, tp1, tp2, _, _ = normalized_levels

    vol_ratio = float(w1h.item(-1, "volume_ratio20") or 1.0)
    rsi = float(w1h.item(-1, "rsi14") or 50.0)
    score = _compute_dynamic_score(
        direction=direction,
        base_score=base_score,
        vol_ratio=vol_ratio,
        rsi=rsi,
    )
    if flow_details.get("orderflow_conflict"):
        score *= float(
            dynamic_params.get(
                "orderflow_conflict_penalty",
                defaults["orderflow_conflict_penalty"],
            )
        )

    reasons = [
        f"Turtle soup {direction}: roll_high={rolling_high:.4f} roll_low={rolling_low:.4f}",
        (
            f"wick_extreme={wick_extreme:.4f} close={confirm_close:.4f} "
            f"limit_entry={entry_price:.4f} sweep_lag={sweep_lag}"
        ),
        f"15m vol_ratio={vol_ratio_15m:.2f}",
    ]
    if flow_details.get("orderflow_conflict"):
        reasons.append("orderflow_conflict_penalty")

    return _build_signal(
        prepared=prepared,
        setup_id=setup_id,
        direction=direction,
        score=score,
        timeframe="15m+1h",
        reasons=reasons,
        strategy_family=family,
        stop=stop,
        tp1=tp1,
        tp2=tp2,
        price_anchor=entry_price,
        atr=atr,
    )


def detect_turtle_soup_setup(
    prepared: PreparedSymbol,
    settings: BotSettings,
    defaults: dict[str, float],
    effective: dict[str, float],
    setup_id: str,
    family: str,
) -> Signal | None:
    spec_kwargs = None
    return run_setup_detection(
        prepared=prepared,
        settings=settings,
        setup_id=setup_id,
        family=family,
        defaults=defaults,
        effective=effective,
        spec_detect=detect_turtle_soup,
        extended_detect=_detect_turtle_soup_extended,
        spec_kwargs=spec_kwargs,
    )


__all__ = ["_detect_turtle_soup_extended", "detect_turtle_soup", "detect_turtle_soup_setup"]


class TurtleSoupSetup(SpecDetectorSetup):
    setup_id = "turtle_soup"
    family = "reversal"
    confirmation_profile = "countertrend_exhaustion"
    required_context = ("futures_flow",)

    DEFAULTS: ClassVar[dict[str, float]] = {
        "base_score": 0.60,
        "roll_bars": 20.0,
        "break_atr_mult": 0.1,
        "sl_buffer_atr": 0.5,
        "volume_threshold": 1.0,
        "bias_mismatch_penalty": 0.75,
        "min_rr": 1.9,
        "min_recovery_delta_long": 0.49,
        "max_recovery_delta_short": 0.51,
        "max_adverse_depth_imbalance": 0.05,
        "max_adverse_microprice_bias": 0.05,
        "false_breakout_lookback_1h": 3,
        "confirmation_lookback_15m": 4,
        "orderflow_conflict_penalty": 0.88,
    }

    detect_setup = detect_turtle_soup_setup

    def detect(self, prepared: PreparedSymbol, settings: BotSettings) -> Signal | None:
        return super().detect(prepared, settings)


__all__ = ["TurtleSoupSetup"]
