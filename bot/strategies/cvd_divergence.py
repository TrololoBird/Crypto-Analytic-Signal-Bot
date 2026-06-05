"""cvd_divergence - canonical strategy detector."""

from __future__ import annotations

import logging
import math
from typing import TYPE_CHECKING, Any, ClassVar, cast

from ..features.prepare import _swing_points
from ..setups import _build_signal, _compute_dynamic_score, _reject
from ..setups.spec_runtime import SpecDetectorSetup, run_setup_detection
from ._common import SpecHit, _pivot_rows, as_float, with_spec_columns

if TYPE_CHECKING:
    import polars as pl

    from ..domain.config import BotSettings
    from ..domain.schemas import PreparedSymbol, Signal

LOG = logging.getLogger("bot.strategies.cvd_divergence")

__all__ = ["detect_cvd_divergence"]


def detect_cvd_divergence(frame: pl.DataFrame, *, timeframe: str = "15m") -> SpecHit | None:
    work = with_spec_columns(frame)
    if work.height < 60:
        return None
    atr = as_float(work.item(-1, "spec_atr14"))
    delta_std = as_float(work.item(-1, "spec_delta_std20"))
    if atr <= 0.0 or delta_std <= 0.0:
        return None
    lows = _pivot_rows(work, price_column="low", indicator_column="spec_cvd", pivot="low")
    if len(lows) >= 2:
        old, new = lows[-2], lows[-1]
        cvd_shift = new["indicator"] - old["indicator"]
        if new["price"] < old["price"] and cvd_shift > 1.5 * delta_std:
            return SpecHit(
                strategy="cvd_divergence",
                direction="long",
                entry=as_float(work.item(-1, "close")),
                stop_basis=new["price"],
                atr=atr,
                timeframe=timeframe,
                reasons=(f"price_ll_cvd_hl shift={cvd_shift:.4f}",),
                rsi=as_float(work.item(-1, "rsi14"), 50.0),
            )
    highs = _pivot_rows(work, price_column="high", indicator_column="spec_cvd", pivot="high")
    if len(highs) >= 2:
        old, new = highs[-2], highs[-1]
        cvd_shift = old["indicator"] - new["indicator"]
        if new["price"] > old["price"] and cvd_shift > 1.5 * delta_std:
            return SpecHit(
                strategy="cvd_divergence",
                direction="short",
                entry=as_float(work.item(-1, "close")),
                stop_basis=new["price"],
                atr=atr,
                timeframe=timeframe,
                reasons=(f"price_hh_cvd_lh shift={cvd_shift:.4f}",),
                rsi=as_float(work.item(-1, "rsi14"), 50.0),
            )
    return None


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


def _detect_cvd_divergence_extended(
    prepared: PreparedSymbol,
    _settings: BotSettings,
    defaults: dict[str, float],
    effective: dict[str, float],
    setup_id: str,
    family: str,
) -> Signal | None:
    dynamic_params = effective
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
        _reject(prepared, setup_id, "insufficient_15m_bars", bars=w.height)
        return None

    if "delta_ratio" not in w.columns:
        _reject(prepared, setup_id, "delta_ratio_missing")
        return None
    delta_series = w["delta_ratio"].drop_nulls()
    if delta_series.len() < 10:
        _reject(
            prepared,
            setup_id,
            "delta_history_insufficient",
            samples=delta_series.len(),
        )
        return None

    atr = float(w.item(-1, "atr14") or 0.0)
    if atr <= 0 or math.isnan(atr):
        _reject(prepared, setup_id, "atr_invalid", atr=atr)
        return None

    price = prepared.mark_price or prepared.universe.last_price
    if not price or price <= 0:
        _reject(prepared, setup_id, "price_missing")
        return None

    closes = w["close"].to_numpy()
    highs = w["high"].to_numpy()
    lows = w["low"].to_numpy()
    delta_vals = _signed_delta_values(w["delta_ratio"].to_numpy())

    split = max(5, divergence_lookback)
    compare = split * 2
    if w.height < compare:
        _reject(prepared, setup_id, "cvd_window_too_short")
        return None
    # split/compare windows pair the previous price leg against the latest leg.
    window_a = closes[-compare:-split]
    window_b = closes[-split:]
    if len(window_a) < 5 or len(window_b) < 5:
        _reject(prepared, setup_id, "cvd_window_too_short")
        return None
    assert len(window_a) >= 5
    assert len(window_b) >= 5
    high_window_b = highs[-split:]
    low_window_b = lows[-split:]
    delta_a = delta_vals[-compare:-split]
    delta_b = delta_vals[-split:]

    if (
        len(window_a) < 5
        or len(window_b) < 5
        or len(delta_a) != len(window_a)
        or len(delta_b) != len(window_b)
    ):
        _reject(prepared, setup_id, "divergence_window_insufficient")
        return None

    price_hh = float(max(window_b)) > float(max(window_a))
    price_ll = float(min(window_b)) < float(min(window_a))
    delta_mean_a = float(cast("Any", delta_a).mean())
    delta_mean_b = float(cast("Any", delta_b).mean())
    delta_shift = delta_mean_b - delta_mean_a

    if math.isnan(delta_mean_a) or math.isnan(delta_mean_b):
        _reject(
            prepared,
            setup_id,
            "delta_mean_invalid",
            delta_mean_a=delta_mean_a,
            delta_mean_b=delta_mean_b,
        )
        return None
    if abs(delta_shift) < min_delta_threshold:
        _reject(
            prepared,
            setup_id,
            "delta_shift_too_small",
            delta_shift=delta_shift,
            min_delta_threshold=min_delta_threshold,
        )
        return None

    direction = None
    bias_penalty = False

    # Use 1H context for 15M signals (not 4H - too lagging for <4h trades)
    bias_1h = getattr(prepared, "bias_1h", prepared.bias_4h)

    # Bearish divergence: price HH, delta declining
    if price_hh and delta_mean_b < delta_mean_a:
        # Don't short in 1H uptrend unless delta very extreme
        bias_override_threshold = max(0.2, min_delta_threshold)
        if bias_1h == "uptrend" and delta_shift > -bias_override_threshold:
            bias_penalty = True
        direction = "short"

    # Bullish divergence: price LL, delta rising
    elif price_ll and delta_mean_b > delta_mean_a:
        bias_override_threshold = max(0.2, min_delta_threshold)
        if bias_1h == "downtrend" and delta_shift < bias_override_threshold:
            bias_penalty = True
        direction = "long"

    if direction is None:
        _reject(prepared, setup_id, "no_cvd_divergence_detected")
        return None

    # --- Compute structural SL/TP ---
    if direction == "long":
        # SL: below the most recent price-action low in window_b + ATR buffer.
        div_extreme = float(min(low_window_b))
        entry_price = div_extreme
        stop = div_extreme - atr * sl_buffer_atr
        risk = entry_price - stop
        if risk <= 0:
            _reject(
                prepared,
                setup_id,
                "risk_non_positive_long",
                stop=stop,
                price=entry_price,
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
            tp2_cands = sh_prices.filter(sh_prices > entry_price)
            tp2 = float(tp2_cands[0]) if tp2_cands.len() > 0 else None
    else:
        # SL: above the most recent price-action high in window_b + ATR buffer.
        div_extreme = float(max(high_window_b))
        entry_price = div_extreme
        stop = div_extreme + atr * sl_buffer_atr
        risk = stop - entry_price
        if risk <= 0:
            _reject(
                prepared,
                setup_id,
                "risk_non_positive_short",
                stop=stop,
                price=entry_price,
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
            tp2_cands = sl_prices.filter(sl_prices < entry_price)
            tp2 = float(tp2_cands[-1]) if tp2_cands.len() > 0 else None

    # Validate: TP1 must clear the configured R threshold.
    if tp1 is None or abs(tp1 - entry_price) < risk * min_rr:
        tp1 = entry_price + risk * min_rr if direction == "long" else entry_price - risk * min_rr
        target_note = f"tp1_rr_fallback_{min_rr:.2f}"
    else:
        target_note = "tp1_prior_segment"
    if tp2 is None or abs(tp2 - entry_price) <= abs(tp1 - entry_price):
        tp2 = (
            entry_price + risk * max(2.0, min_rr + 0.35)
            if direction == "long"
            else entry_price - risk * max(2.0, min_rr + 0.35)
        )

    vol_ratio = float(w.item(-1, "volume_ratio20") or 1.0)
    rsi = float(w.item(-1, "rsi14") or 50.0)
    score = _compute_dynamic_score(
        direction=direction,
        base_score=base_score,
        vol_ratio=vol_ratio,
        rsi=rsi,
    )
    if bias_penalty:
        score *= float(
            dynamic_params.get("bias_mismatch_penalty", defaults["bias_mismatch_penalty"])
        )

    reasons = [
        f"CVD divergence {direction}",
        f"delta_a={delta_mean_a:.3f} delta_b={delta_mean_b:.3f} shift={delta_shift:.3f}",
        f"bias_1h={bias_1h}",
        f"limit_entry={entry_price:.4f}",
        target_note,
    ]
    if bias_penalty:
        reasons.append("bias_mismatch_penalty")

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


def detect_cvd_divergence_setup(
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
        spec_detect=detect_cvd_divergence,
        extended_detect=_detect_cvd_divergence_extended,
        spec_kwargs=spec_kwargs,
    )


__all__ = [
    "_detect_cvd_divergence_extended",
    "detect_cvd_divergence",
    "detect_cvd_divergence_setup",
]


class CVDDivergenceSetup(SpecDetectorSetup):
    setup_id = "cvd_divergence"
    family = "reversal"
    confirmation_profile = "countertrend_exhaustion"
    required_context = ("futures_flow",)

    DEFAULTS: ClassVar[dict[str, float]] = {
        "base_score": 0.5,
        "divergence_lookback": 5,
        "delta_lookback": 3,
        "bias_mismatch_penalty": 0.75,
        "min_rr": 1.9,
        "min_delta_threshold": 0.06,
        "sl_buffer_atr": 0.5,
    }

    detect_setup = detect_cvd_divergence_setup

    def detect(self, prepared: PreparedSymbol, settings: BotSettings) -> Signal | None:
        return super().detect(prepared, settings)


__all__ = ["CVDDivergenceSetup"]
