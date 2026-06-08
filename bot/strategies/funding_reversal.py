from __future__ import annotations

import logging
import math
from typing import TYPE_CHECKING, ClassVar

from ..features.prepare import _swing_points
from ..setups import _build_signal, _compute_dynamic_score, _reject
from ._common import as_float as _as_float
from ._common import confirmed_pattern_frame
from .roadmap_base import RoadmapSetup

if TYPE_CHECKING:
    from ..domain.config import BotSettings
    from ..domain.schemas import PreparedSymbol, Signal

LOG = logging.getLogger(__name__)

__all__ = ["detect_funding_reversal"]


def detect_funding_reversal(
    prepared: PreparedSymbol,
    _settings: BotSettings,
    effective_params: dict[str, float],
    *,
    setup_id: str,
    family: str,
) -> Signal | None:
    defaults = effective_params
    funding_threshold = _as_float(
        defaults.get("funding_threshold", 0.0010),
        defaults["funding_threshold"],
    )
    _as_float(
        defaults.get("funding_soft_threshold", defaults["funding_soft_threshold"]),
        defaults["funding_soft_threshold"],
    )
    # Soft threshold is for relative-tier scoring only - not the primary extreme gate.
    funding_cap = getattr(prepared, "funding_rate_cap", None)
    funding_floor = getattr(prepared, "funding_rate_floor", None)
    dynamic_mult = _as_float(defaults.get("funding_dynamic_threshold_mult", 0.65), 0.65)
    try:
        cap_extreme = abs(float(funding_cap)) if funding_cap is not None else 0.0
    except (TypeError, ValueError):
        cap_extreme = 0.0
    try:
        floor_extreme = abs(float(funding_floor)) if funding_floor is not None else 0.0
    except (TypeError, ValueError):
        floor_extreme = 0.0
    relevant_extreme = max(cap_extreme, floor_extreme)
    if relevant_extreme > 0.0:
        dynamic_threshold = max(
            funding_threshold * 0.40,
            min(funding_threshold * 1.50, relevant_extreme * dynamic_mult),
        )
    else:
        dynamic_threshold = funding_threshold
    effective_threshold = max(0.00002, dynamic_threshold)
    funding_trend_bars = int(defaults.get("funding_trend_bars", defaults["funding_trend_bars"]))
    funding_recent_extreme_lookback_hours = _as_float(
        defaults.get(
            "funding_recent_extreme_lookback_hours",
            defaults["funding_recent_extreme_lookback_hours"],
        ),
        defaults["funding_recent_extreme_lookback_hours"],
    )
    historical_funding_score_penalty = _as_float(
        defaults.get(
            "historical_funding_score_penalty",
            defaults["historical_funding_score_penalty"],
        ),
        defaults["historical_funding_score_penalty"],
    )
    relative_funding_score_penalty = _as_float(
        defaults.get(
            "relative_funding_score_penalty",
            defaults["relative_funding_score_penalty"],
        ),
        defaults["relative_funding_score_penalty"],
    )
    min_delta_threshold = _as_float(
        defaults.get("min_delta_threshold", defaults["min_delta_threshold"]),
        defaults["min_delta_threshold"],
    )
    sl_buffer_atr = _as_float(
        defaults.get("sl_buffer_atr", defaults["sl_buffer_atr"]),
        defaults["sl_buffer_atr"],
    )
    base_score = _as_float(
        defaults.get("base_score", defaults["base_score"]),
        defaults["base_score"],
    )
    min_rr = _as_float(defaults.get("min_rr", defaults["min_rr"]), defaults["min_rr"])
    confirmation_lookback = max(
        2,
        int(
            defaults.get(
                "confirmation_lookback_bars",
                defaults["confirmation_lookback_bars"],
            )
        ),
    )
    min_confirmation_score = _as_float(
        defaults.get("min_confirmation_score", defaults["min_confirmation_score"]),
        defaults["min_confirmation_score"],
    )
    min_volume_ratio = _as_float(
        defaults.get("min_volume_ratio", defaults["min_volume_ratio"]),
        defaults["min_volume_ratio"],
    )

    if prepared.funding_rate is None:
        _reject(prepared, setup_id, "data.funding_rate_missing")
        return None
    fr = prepared.funding_rate
    recent_extreme_rate = getattr(prepared, "funding_recent_extreme_rate", None)
    recent_extreme_age_hours = getattr(
        prepared,
        "funding_recent_extreme_age_hours",
        None,
    )
    effective_fr = fr
    funding_source = "current"
    funding_score_penalty = 1.0
    if math.isnan(fr) or abs(fr) <= effective_threshold:
        try:
            recent_rate = None if recent_extreme_rate is None else float(recent_extreme_rate)
            recent_age = (
                None if recent_extreme_age_hours is None else float(recent_extreme_age_hours)
            )
        except (TypeError, ValueError):
            recent_rate = None
            recent_age = None
        if (
            recent_rate is not None
            and recent_age is not None
            and recent_age <= funding_recent_extreme_lookback_hours
            and abs(recent_rate) > effective_threshold
        ):
            effective_fr = recent_rate
            funding_source = "history"
            funding_score_penalty = max(0.70, min(1.0, historical_funding_score_penalty))
        else:
            _reject(
                prepared,
                setup_id,
                "indicator.funding_not_extreme",
                funding_rate=fr,
                funding_threshold=funding_threshold,
                effective_threshold=effective_threshold,
                recent_extreme_rate=recent_extreme_rate,
                recent_extreme_age_hours=recent_extreme_age_hours,
                lookback_hours=funding_recent_extreme_lookback_hours,
            )
            return None
    elif abs(fr) <= funding_threshold:
        funding_source = "relative"
        funding_score_penalty = max(0.60, min(1.0, relative_funding_score_penalty))

    funding_trend = prepared.funding_trend

    w = confirmed_pattern_frame(prepared.work_15m)
    if w.height < 5:
        _reject(prepared, setup_id, "insufficient_15m_bars", bars=w.height)
        return None

    atr = _as_float(w.item(-1, "atr14"))
    if atr <= 0 or math.isnan(atr):
        _reject(prepared, setup_id, "atr_invalid", atr=atr)
        return None

    price = prepared.mark_price or prepared.universe.last_price
    if not price or price <= 0:
        _reject(prepared, setup_id, "price_missing")
        return None

    vol_ratio = _as_float(w.item(-1, "volume_ratio20"), 1.0)

    latest_delta_ratio: float | None = None
    delta_shift = 0.0
    if "delta_ratio" in w.columns:
        delta_series = w["delta_ratio"].drop_nulls()
        if delta_series.len() > 0:
            latest_delta_ratio = _as_float(delta_series[-1], 0.5)
            delta_shift = latest_delta_ratio - 0.5

    recent = w.tail(min(confirmation_lookback, w.height))
    trend_window = max(6, funding_trend_bars * 3)
    rsi = _as_float(w.item(-1, "rsi14"), 50.0)
    bb_pct_b = _as_float(w.item(-1, "bb_pct_b"), 0.5) if "bb_pct_b" in w.columns else 0.5
    close_position = (
        _as_float(w.item(-1, "close_position"), 0.5) if "close_position" in w.columns else 0.5
    )

    direction = "short" if effective_fr > effective_threshold else "long"
    confirmation_score = 0.0
    confirmation_reasons: list[str] = []
    for idx in range(recent.height):
        bar_open = _as_float(recent.item(idx, "open"))
        bar_close = _as_float(recent.item(idx, "close"))
        bar_high = _as_float(recent.item(idx, "high"))
        bar_low = _as_float(recent.item(idx, "low"))
        body = abs(bar_close - bar_open)
        upper_wick = bar_high - max(bar_open, bar_close)
        lower_wick = min(bar_open, bar_close) - bar_low
        if direction == "short" and (
            bar_close < bar_open
            or (body > 0.0 and upper_wick >= body * 1.2)
            or upper_wick >= atr * 0.35
        ):
            confirmation_score += 0.75
            confirmation_reasons.append("bearish_recent_reversal_bar")
            break
        if direction == "long" and (
            bar_close > bar_open
            or (body > 0.0 and lower_wick >= body * 1.2)
            or lower_wick >= atr * 0.35
        ):
            confirmation_score += 0.75
            confirmation_reasons.append("bullish_recent_reversal_bar")
            break

    if direction == "short":
        if rsi >= 58.0:
            confirmation_score += 0.35
            confirmation_reasons.append(f"rsi={rsi:.1f}")
        if bb_pct_b >= 0.80 or close_position >= 0.65:
            confirmation_score += 0.35
            confirmation_reasons.append("price_extended_up")
        if latest_delta_ratio is not None and delta_shift <= -min_delta_threshold:
            confirmation_score += 0.35
            confirmation_reasons.append(f"delta_shift={delta_shift:.3f}")
        entry_price = _as_float(recent["high"].max(), price)
        stop = entry_price + atr * sl_buffer_atr
        risk = stop - entry_price
        trend_start = max(0, w.height - (trend_window + 1))
        tp1 = _as_float(w["low"].slice(trend_start, trend_window).min())
        w1h = confirmed_pattern_frame(prepared.work_1h)
        tp2 = None
        if w1h.height > 5:
            _, sl_mask = _swing_points(w1h, n=3, include_unconfirmed_tail=True)
            sl_prices = w1h.filter(sl_mask)["low"]
            tp2_cands = sl_prices.filter(sl_prices < entry_price)
            tp2 = _as_float(tp2_cands[-1]) if tp2_cands.len() > 0 else None
    else:
        if rsi <= 42.0:
            confirmation_score += 0.35
            confirmation_reasons.append(f"rsi={rsi:.1f}")
        if bb_pct_b <= 0.20 or close_position <= 0.35:
            confirmation_score += 0.35
            confirmation_reasons.append("price_extended_down")
        if latest_delta_ratio is not None and delta_shift >= min_delta_threshold:
            confirmation_score += 0.35
            confirmation_reasons.append(f"delta_shift={delta_shift:.3f}")
        entry_price = _as_float(recent["low"].min(), price)
        stop = entry_price - atr * sl_buffer_atr
        risk = entry_price - stop
        trend_start = max(0, w.height - (trend_window + 1))
        tp1 = _as_float(w["high"].slice(trend_start, trend_window).max())
        w1h = confirmed_pattern_frame(prepared.work_1h)
        tp2 = None
        if w1h.height > 5:
            sh_mask, _ = _swing_points(w1h, n=3, include_unconfirmed_tail=True)
            sh_prices = w1h.filter(sh_mask)["high"]
            tp2_cands = sh_prices.filter(sh_prices > entry_price)
            tp2 = _as_float(tp2_cands[0]) if tp2_cands.len() > 0 else None

    funding_cap = getattr(prepared, "funding_rate_cap", None)
    funding_floor = getattr(prepared, "funding_rate_floor", None)
    if direction == "short" and funding_cap is not None:
        try:
            cap = abs(float(funding_cap))
        except (TypeError, ValueError):
            cap = 0.0
        if cap > 0.0 and abs(effective_fr) >= cap * 0.75:
            confirmation_score += 0.15
            confirmation_reasons.append(f"near_funding_cap={cap:.5f}")
    elif direction == "long" and funding_floor is not None:
        try:
            floor = abs(float(funding_floor))
        except (TypeError, ValueError):
            floor = 0.0
        if floor > 0.0 and abs(effective_fr) >= floor * 0.75:
            confirmation_score += 0.15
            confirmation_reasons.append(f"near_funding_floor={-floor:.5f}")

    if vol_ratio >= min_volume_ratio:
        confirmation_score += 0.25
        confirmation_reasons.append(f"vol_ratio={vol_ratio:.2f}")
    min_oi_change = _as_float(
        defaults.get("min_oi_change_pct", defaults.get("min_oi_change_pct", 0.5)),
        0.5,
    )
    oi_penalty = 1.0
    if prepared.oi_change_pct is not None:
        oi_change = _as_float(prepared.oi_change_pct, 0.0)
        if abs(oi_change) < min_oi_change:
            oi_penalty = _as_float(
                defaults.get("oi_unconfirmed_penalty", defaults["oi_unconfirmed_penalty"]),
                defaults["oi_unconfirmed_penalty"],
            )
            confirmation_reasons.append(f"oi_change_weak={oi_change:.2f}")
        elif direction == "short" and oi_change <= 0.0:
            oi_penalty = 0.92
            confirmation_reasons.append("oi_not_building_longs")
        elif direction == "long" and oi_change <= 0.0:
            oi_penalty = 0.92
            confirmation_reasons.append("oi_not_building_shorts")
    if funding_trend == "flat":
        confirmation_score *= 0.95
        confirmation_reasons.append("funding_trend_flat_penalty")
    elif (direction == "short" and funding_trend == "falling") or (
        direction == "long" and funding_trend == "rising"
    ):
        confirmation_score *= 0.90
        confirmation_reasons.append("funding_unwinding_penalty")

    if confirmation_score < min_confirmation_score:
        _reject(
            prepared,
            setup_id,
            "funding_reversal_confirmation_missing",
            direction=direction,
            confirmation_score=round(confirmation_score, 3),
            min_confirmation_score=min_confirmation_score,
            rsi=rsi,
            bb_pct_b=bb_pct_b,
            close_position=close_position,
            delta_shift=delta_shift,
            vol_ratio=vol_ratio,
        )
        return None
    if risk <= 0:
        _reject(
            prepared,
            setup_id,
            f"risk_non_positive_{direction}",
            stop=stop,
            price=entry_price,
        )
        return None

    fallback_note = None
    if tp1 is None or abs(tp1 - entry_price) < risk * min_rr:
        tp1 = entry_price + risk * min_rr if direction == "long" else entry_price - risk * min_rr
        fallback_note = f"tp1_rr_fallback_{min_rr:.2f}"
    if tp2 is None or abs(tp2 - entry_price) <= abs(tp1 - entry_price):
        tp2 = (
            entry_price + risk * max(2.0, min_rr + 0.35)
            if direction == "long"
            else entry_price - risk * max(2.0, min_rr + 0.35)
        )

    score = _compute_dynamic_score(
        direction=direction,
        base_score=base_score,
        vol_ratio=vol_ratio,
        rsi=rsi,
    )
    score = min(1.0, score * min(1.20, max(0.80, 0.85 + confirmation_score * 0.10)))
    score *= funding_score_penalty
    score *= oi_penalty

    reasons = [
        (
            f"Funding reversal {direction}: fr={effective_fr:.5f} "
            f"source={funding_source} threshold={effective_threshold:.5f} "
            f"trend={funding_trend or 'unknown'}"
        ),
        f"current_funding={fr:.5f}",
        f"confirmation_score={confirmation_score:.2f} trend_window={trend_window}",
        f"limit_entry={entry_price:.4f}",
        f"sl_buffer_atr={sl_buffer_atr:.2f} min_rr={min_rr:.2f}",
        *confirmation_reasons,
    ]
    if funding_source == "history":
        reasons.append(f"funding_extreme_age_h={recent_extreme_age_hours:.1f}")
    if fallback_note:
        reasons.append(fallback_note)

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


class FundingReversalSetup(RoadmapSetup):
    setup_id = "funding_reversal"
    ENTRY_ORDER_TYPE: ClassVar[str] = "market"
    family = "reversal"
    confirmation_profile = "countertrend_exhaustion"
    required_context = ("futures_flow",)
    requires_funding = True

    DEFAULTS: ClassVar[dict[str, float]] = {
        **RoadmapSetup.DEFAULTS,
        "base_score": 0.60,
        "funding_threshold": 0.0010,
        "funding_dynamic_threshold_mult": 0.65,
        "funding_soft_threshold": 0.00010,
        "funding_trend_bars": 3.0,
        "funding_recent_extreme_lookback_hours": 48.0,
        "historical_funding_score_penalty": 0.92,
        "relative_funding_score_penalty": 0.82,
        "min_delta_threshold": 0.02,
        "confirmation_lookback_bars": 4,
        "min_confirmation_score": 0.70,
        "min_volume_ratio": 0.85,
        "sl_buffer_atr": 0.6,
        "bias_mismatch_penalty": 0.75,
        "min_rr": 1.9,
        "min_oi_change_pct": 0.5,
        "oi_unconfirmed_penalty": 0.90,
    }

    def detect(self, prepared: PreparedSymbol, settings: BotSettings) -> Signal | None:
        return detect_funding_reversal(
            prepared,
            settings,
            self._params(prepared, settings),
            setup_id=self.setup_id,
            family=self.family,
        )


__all__ = ["FundingReversalSetup"]
