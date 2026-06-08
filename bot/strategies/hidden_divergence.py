"""hidden_divergence - canonical strategy detector."""

from __future__ import annotations

import logging
import math
from typing import TYPE_CHECKING, ClassVar

from ..features.prepare import _swing_points
from ..setups import _build_signal, _compute_dynamic_score, _reject
from ..setups.spec_runtime import SpecDetectorSetup, run_setup_detection
from ._common import (
    SpecHit,
    _latest_values,
    _pivot_rows,
    as_float,
    confirmed_pattern_frame,
    with_spec_columns,
)

if TYPE_CHECKING:
    import polars as pl

    from ..domain.config import BotSettings
    from ..domain.schemas import PreparedSymbol, Signal

LOG = logging.getLogger("bot.strategies.hidden_divergence")

__all__ = ["detect_hidden_divergence"]


def detect_hidden_divergence(frame: pl.DataFrame, *, timeframe: str = "15m") -> SpecHit | None:
    work = with_spec_columns(frame)
    if work.height < 60:
        return None
    row = _latest_values(work)
    atr = row.get("spec_atr14", 0.0)
    if atr <= 0.0:
        return None
    min_rsi_separation = 4.0
    lows = _pivot_rows(work, price_column="low", indicator_column="rsi14", pivot="low")
    if row["close"] > row.get("spec_ema50", row["close"]) and len(lows) >= 2:
        old, new = lows[-2], lows[-1]
        rsi_gap = old["indicator"] - new["indicator"]
        if new["price"] > old["price"] and rsi_gap >= min_rsi_separation:
            return SpecHit(
                strategy="hidden_divergence",
                direction="long",
                entry=new["price"],
                stop_basis=new["price"],
                atr=atr,
                timeframe=timeframe,
                reasons=(
                    f"hidden_bullish_div price_hl={new['price']:.4f}",
                    f"rsi_ll_gap={rsi_gap:.2f}",
                ),
                rsi=row.get("rsi14", 50.0),
            )
    highs = _pivot_rows(work, price_column="high", indicator_column="rsi14", pivot="high")
    if row["close"] < row.get("spec_ema50", row["close"]) and len(highs) >= 2:
        old, new = highs[-2], highs[-1]
        rsi_gap = new["indicator"] - old["indicator"]
        if new["price"] < old["price"] and rsi_gap >= min_rsi_separation:
            return SpecHit(
                strategy="hidden_divergence",
                direction="short",
                entry=new["price"],
                stop_basis=new["price"],
                atr=atr,
                timeframe=timeframe,
                reasons=(
                    f"hidden_bearish_div price_lh={new['price']:.4f}",
                    f"rsi_hh_gap={rsi_gap:.2f}",
                ),
                rsi=row.get("rsi14", 50.0),
            )
    return None


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
) -> tuple[float, float] | None:
    price_values = _finite_series_values(prices)
    oscillator_values = _finite_series_values(oscillators)
    count = min(len(price_values), len(oscillator_values))
    if count < 2:
        return None
    price_values = price_values[-count:]
    oscillator_values = oscillator_values[-count:]
    previous_price = price_values[-2]
    current_price = price_values[-1]
    previous_oscillator = oscillator_values[-2]
    current_oscillator = oscillator_values[-1]
    if direction == "long":
        oscillator_gap = previous_oscillator - current_oscillator
        if current_price > previous_price and oscillator_gap >= min_oscillator_separation:
            return current_price, oscillator_gap
    else:
        oscillator_gap = current_oscillator - previous_oscillator
        if current_price < previous_price and oscillator_gap >= min_oscillator_separation:
            return current_price, oscillator_gap
    return None


def _detect_hidden_divergence_extended(
    prepared: PreparedSymbol,
    _settings: BotSettings,
    defaults: dict[str, float],
    effective: dict[str, float],
    setup_id: str,
    family: str,
) -> Signal | None:
    dynamic_params = effective
    # FIX 2026-05-21: spec divergence only checks the latest 15m pivots; on
    # a miss, keep the existing 1h confirmed-swing hidden divergence scan.
    rsi_divergence_lookback = int(
        dynamic_params.get("rsi_divergence_lookback", defaults["rsi_divergence_lookback"])
    )
    rsi_divergence_threshold = float(
        dynamic_params.get("rsi_divergence_threshold", defaults["rsi_divergence_threshold"])
    )
    min_delta_threshold = float(
        dynamic_params.get("min_delta_threshold", defaults["min_delta_threshold"])
    )
    min_volume_ratio = float(dynamic_params.get("min_volume_ratio", defaults["min_volume_ratio"]))
    sl_buffer_atr = float(dynamic_params.get("sl_buffer_atr", defaults["sl_buffer_atr"]))

    w1h = confirmed_pattern_frame(prepared.work_1h)
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

    w15m = confirmed_pattern_frame(prepared.work_15m)
    if w15m.height < 3:
        _reject(prepared, setup_id, "insufficient_15m_bars", bars=w15m.height)
        return None
    vol_ratio_15m = float(w15m.item(-1, "volume_ratio20") or 1.0)
    volume_penalty = False
    if vol_ratio_15m < min_volume_ratio:
        volume_penalty = True

    bias_1h = getattr(prepared, "bias_1h", prepared.bias_4h)
    close_1h = float(w1h.item(-1, "close") or 0.0)
    ema20_1h = float(w1h.item(-1, "ema20") or 0.0) if "ema20" in w1h.columns else 0.0
    ema50_1h = float(w1h.item(-1, "ema50") or 0.0) if "ema50" in w1h.columns else 0.0
    if min(close_1h, ema50_1h) <= 0.0:
        _reject(prepared, setup_id, "indicator.trend_context_missing")
        return None
    long_trend_context = (
        close_1h > ema50_1h and (ema20_1h <= 0.0 or ema20_1h >= ema50_1h) and bias_1h != "downtrend"
    )
    short_trend_context = (
        close_1h < ema50_1h and (ema20_1h <= 0.0 or ema20_1h <= ema50_1h) and bias_1h != "uptrend"
    )
    if not long_trend_context and not short_trend_context:
        _reject(
            prepared,
            setup_id,
            "pattern.no_trend_context",
            bias_1h=bias_1h,
            close_1h=close_1h,
            ema50_1h=ema50_1h,
        )
        return None

    sh_mask, sl_mask = _swing_points(
        w1h, n=max(2, rsi_divergence_lookback), include_unconfirmed_tail=False
    )
    sh_prices = w1h.filter(sh_mask)["high"]
    sh_rsi = w1h.filter(sh_mask)["rsi14"] if "rsi14" in w1h.columns else None
    sl_prices = w1h.filter(sl_mask)["low"]
    sl_rsi = w1h.filter(sl_mask)["rsi14"] if "rsi14" in w1h.columns else None

    direction = None
    stop_price = None
    swing_ref = None
    rsi_separation = 0.0

    # Hidden Bullish: price HL (sl[-1] > sl[-2]) + RSI LL (rsi_sl[-1] < rsi_sl[-2])
    impulse_size = None
    swing_ref = None
    if long_trend_context and sl_prices.len() >= 2 and sl_rsi is not None and sl_rsi.len() >= 2:
        match = _find_recent_hidden_divergence_pair(
            sl_prices,
            sl_rsi,
            direction="long",
            min_oscillator_separation=rsi_divergence_threshold,
        )
        if match is not None:
            direction = "long"
            swing_ref, rsi_separation = match
            # Compute last impulse wave size for Fib extensions
            if sh_prices.len() >= 1:
                impulse_size = abs(float(sh_prices.to_numpy()[-1]) - float(swing_ref))

    # Hidden Bearish: price LH (sh[-1] < sh[-2]) + RSI HH (rsi_sh[-1] > rsi_sh[-2])
    if direction is None and (
        short_trend_context and sh_prices.len() >= 2 and sh_rsi is not None and sh_rsi.len() >= 2
    ):
        match = _find_recent_hidden_divergence_pair(
            sh_prices,
            sh_rsi,
            direction="short",
            min_oscillator_separation=rsi_divergence_threshold,
        )
        if match is not None:
            direction = "short"
            swing_ref, rsi_separation = match
            if sl_prices.len() >= 1:
                impulse_size = abs(float(swing_ref) - float(sl_prices.to_numpy()[-1]))

    if direction is None or swing_ref is None:
        _reject(
            prepared,
            setup_id,
            "pattern.no_hidden_divergence",
            swing_lows=sl_prices.len(),
            swing_highs=sh_prices.len(),
            min_rsi_separation=rsi_divergence_threshold,
        )
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
    if direction == "long" and latest_delta_ratio is not None and delta_shift < min_delta_threshold:
        delta_penalty = True
    if (
        direction == "short"
        and latest_delta_ratio is not None
        and delta_shift > -min_delta_threshold
    ):
        delta_penalty = True

    # --- Compute structural SL/TP ---
    if direction == "long":
        # SL: beyond hidden divergence extreme (swing low) + 0.15xATR
        entry_price = swing_ref
        stop_price = swing_ref - atr * sl_buffer_atr
        risk = entry_price - stop_price
        if risk <= 0:
            _reject(
                prepared,
                setup_id,
                "risk_non_positive_long",
                stop=stop_price,
                price=entry_price,
            )
            return None
        # TP1/TP2: Fibonacci 1.272x and 1.618x extension of last impulse wave
        if impulse_size and impulse_size > 0:
            tp1 = entry_price + impulse_size * 1.272
            tp2 = entry_price + impulse_size * 1.618
        else:
            tp1 = None
            tp2 = None
    else:
        # SL: beyond hidden divergence extreme (swing high) + 0.15xATR
        entry_price = swing_ref
        stop_price = swing_ref + atr * sl_buffer_atr
        risk = stop_price - entry_price
        if risk <= 0:
            _reject(
                prepared,
                setup_id,
                "risk_non_positive_short",
                stop=stop_price,
                price=entry_price,
            )
            return None
        # TP1/TP2: Fibonacci extensions of last impulse wave
        if impulse_size and impulse_size > 0:
            tp1 = entry_price - impulse_size * 1.272
            tp2 = entry_price - impulse_size * 1.618
        else:
            tp1 = None
            tp2 = None

    min_rr = float(dynamic_params.get("min_rr", defaults["min_rr"]))
    if tp1 is None or abs(tp1 - entry_price) < risk * min_rr:
        tp1 = entry_price + risk * min_rr if direction == "long" else entry_price - risk * min_rr
        reasons_note = f"tp1_rr_fallback_{min_rr:.2f}"
    else:
        reasons_note = "tp1_fib_extension"
    if tp2 is None or abs(tp2 - entry_price) <= abs(tp1 - entry_price):
        tp2 = (
            entry_price + risk * max(2.0, min_rr + 0.35)
            if direction == "long"
            else entry_price - risk * max(2.0, min_rr + 0.35)
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
    stoch_rsi_boost = 1.0
    if "stoch_rsi14" in w15m.columns:
        stoch = float(w15m.item(-1, "stoch_rsi14") or 0.5)
        stoch_reversal = (direction == "long" and stoch < 0.2) or (
            direction == "short" and stoch > 0.8
        )
        if stoch_reversal:
            stoch_rsi_boost = 1.06
    score = min(1.0, score * stoch_rsi_boost)

    reasons = [
        f"Hidden div {direction}: swing_ref={swing_ref:.4f} rsi_sep={rsi_separation:.2f}",
        f"price={price:.4f} limit_entry={entry_price:.4f}",
        f"vol_ratio_15m={vol_ratio_15m:.2f} delta_shift={delta_shift:.3f} 1h={bias_1h}",
        reasons_note,
    ]
    if volume_penalty:
        reasons.append("volume_penalty")
    if delta_penalty:
        reasons.append("delta_mismatch_penalty")

    return _build_signal(
        prepared=prepared,
        setup_id=setup_id,
        direction=direction,
        score=score,
        timeframe="15m+1h",
        reasons=reasons,
        strategy_family=family,
        stop=stop_price,
        tp1=tp1,
        tp2=tp2,
        price_anchor=entry_price,
        atr=atr,
    )


def detect_hidden_divergence_setup(
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
        spec_detect=detect_hidden_divergence,
        extended_detect=_detect_hidden_divergence_extended,
        spec_kwargs=spec_kwargs,
    )


__all__ = [
    "_detect_hidden_divergence_extended",
    "detect_hidden_divergence",
    "detect_hidden_divergence_setup",
]


class HiddenDivergenceSetup(SpecDetectorSetup):
    setup_id = "hidden_divergence"
    ENTRY_ORDER_TYPE: ClassVar[str] = "market"
    family = "continuation"
    confirmation_profile = "trend_follow"
    required_context = ("futures_flow",)

    DEFAULTS: ClassVar[dict[str, float]] = {
        "base_score": 0.62,
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

    detect_setup = detect_hidden_divergence_setup

    def detect(self, prepared: PreparedSymbol, settings: BotSettings) -> Signal | None:
        return super().detect(prepared, settings)


__all__ = ["HiddenDivergenceSetup"]
