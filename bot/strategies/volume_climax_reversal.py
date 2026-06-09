"""volume_climax_reversal - canonical strategy detector."""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, ClassVar

from ..setups import _build_signal, _compute_dynamic_score, _reject
from ..setups.spec_runtime import SpecDetectorSetup, run_setup_detection
from ._common import SpecHit, as_float, confirmed_pattern_frame, with_spec_columns

if TYPE_CHECKING:
    import polars as pl

    from ..domain.config import BotSettings
    from ..domain.schemas import PreparedSymbol, Signal

__all__ = ["detect_volume_climax_reversal"]


def detect_volume_climax_reversal(frame: pl.DataFrame, *, timeframe: str = "15m") -> SpecHit | None:
    work = with_spec_columns(frame)
    if work.height < 30:
        return None
    current_close = as_float(work.item(-1, "close"))
    current_idx = int(work.item(-1, "_spec_idx"))
    recent = work.tail(4).to_dicts()
    for row in reversed(recent[:-1]):
        idx = int(row["_spec_idx"])
        lag = current_idx - idx
        if lag < 1 or lag > 3:
            continue
        atr = as_float(row.get("spec_atr14"))
        volume_mean = as_float(row.get("spec_volume_mean20"))
        if atr <= 0.0 or volume_mean <= 0.0 or as_float(row.get("volume")) <= volume_mean * 5.0:
            continue
        midpoint = (as_float(row.get("high")) + as_float(row.get("low"))) / 2.0
        prev_high = as_float(row.get("spec_prev_high20"))
        prev_low = as_float(row.get("spec_prev_low20"))
        if as_float(row.get("low")) < prev_low and current_close > midpoint:
            return SpecHit(
                strategy="volume_climax_reversal",
                direction="long",
                entry=prev_low,
                stop_basis=as_float(row.get("low")),
                atr=atr,
                timeframe=timeframe,
                reasons=(f"sell_climax_reclaimed_mid={midpoint:.4f}", f"lag={lag}"),
                vol_ratio=as_float(row.get("volume_ratio20"), 1.0),
                rsi=as_float(work.item(-1, "rsi14"), 50.0),
            )
        if as_float(row.get("high")) > prev_high and current_close < midpoint:
            return SpecHit(
                strategy="volume_climax_reversal",
                direction="short",
                entry=prev_high,
                stop_basis=as_float(row.get("high")),
                atr=atr,
                timeframe=timeframe,
                reasons=(f"buy_climax_reclaimed_mid={midpoint:.4f}", f"lag={lag}"),
                vol_ratio=as_float(row.get("volume_ratio20"), 1.0),
                rsi=as_float(work.item(-1, "rsi14"), 50.0),
            )
    return None


def _as_float(value: object, default: float = 0.0) -> float:
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    return default


def _detect_volume_climax_reversal_extended(
    prepared: PreparedSymbol,
    _settings: BotSettings,
    _defaults: dict[str, float],
    effective: dict[str, float],
    setup_id: str,
    family: str,
) -> Signal | None:
    work = confirmed_pattern_frame(prepared.work_15m)
    effective_params = effective
    # FIX 2026-05-21: spec requires a very large recent climax; fall through
    # to the configured wick/body reclaim detector before rejecting.
    if work.height < 30:
        _reject(prepared, setup_id, "insufficient_15m_bars")
        return None

    required = (
        "open",
        "high",
        "low",
        "close",
        "atr14",
        "volume_ratio20",
        "close_position",
        "rsi14",
        "prev_donchian_low20",
        "prev_donchian_high20",
    )
    missing = [column for column in required if column not in work.columns]
    if missing:
        _reject(prepared, setup_id, "missing_columns", missing_fields=missing)
        return None

    atr = _as_float(work.item(-1, "atr14"))
    close = _as_float(work.item(-1, "close"))
    latest_close_position = _as_float(work.item(-1, "close_position"), 0.5)
    rsi = _as_float(work.item(-1, "rsi14"), 50.0)

    if min(close, atr) <= 0.0 or math.isnan(atr):
        _reject(prepared, setup_id, "invalid_indicator_state", atr=atr)
        return None

    min_wick_atr = float(effective_params["min_wick_atr"])
    configured_min_volume = float(effective_params["min_volume_ratio"])
    adaptive_min_volume = float(effective_params.get("adaptive_min_volume_ratio", 1.30))
    strong_wick_multiplier = float(effective_params.get("strong_wick_multiplier", 1.35))
    direction: str | None = None
    clarity = 0.0
    signal_lookback = max(
        1,
        int(effective_params.get("signal_lookback_bars", 3)),
    )
    recent = work.tail(min(signal_lookback, work.height))
    signal_idx = work.height - 1
    signal_high = _as_float(work.item(-1, "high"))
    signal_low = _as_float(work.item(-1, "low"))
    signal_vol_ratio = _as_float(work.item(-1, "volume_ratio20"), 1.0)
    lower_wick_atr = 0.0
    upper_wick_atr = 0.0
    reclaim_level = 0.0
    for local_idx in range(recent.height - 1, -1, -1):
        open_ = _as_float(recent.item(local_idx, "open"))
        high = _as_float(recent.item(local_idx, "high"))
        low = _as_float(recent.item(local_idx, "low"))
        bar_close = _as_float(recent.item(local_idx, "close"))
        prev_low = _as_float(recent.item(local_idx, "prev_donchian_low20"))
        prev_high = _as_float(recent.item(local_idx, "prev_donchian_high20"))
        close_position = _as_float(recent.item(local_idx, "close_position"), 0.5)
        vol_ratio_candidate = _as_float(recent.item(local_idx, "volume_ratio20"), 1.0)
        if min(open_, high, low, bar_close, prev_low, prev_high) <= 0.0:
            continue
        candidate_lower_wick_atr = (min(open_, bar_close) - low) / atr
        candidate_upper_wick_atr = (high - max(open_, bar_close)) / atr
        long_reclaim = max(bar_close, close) > prev_low and latest_close_position >= 0.52
        short_reclaim = min(bar_close, close) < prev_high and latest_close_position <= 0.48
        if (
            low < prev_low
            and long_reclaim
            and candidate_lower_wick_atr >= min_wick_atr
            and close_position >= 0.48
        ):
            direction = "long"
            clarity = min(candidate_lower_wick_atr / 2.0, 1.0)
            lower_wick_atr = candidate_lower_wick_atr
            upper_wick_atr = candidate_upper_wick_atr
            signal_idx = work.height - recent.height + local_idx
            signal_low = low
            signal_high = high
            signal_vol_ratio = max(signal_vol_ratio, vol_ratio_candidate)
            reclaim_level = prev_low
            break
        if (
            candidate_lower_wick_atr >= min_wick_atr * 1.35
            and close >= bar_close * 0.996
            and close_position >= 0.45
            and rsi <= float(effective_params["max_rsi_long"]) + 15.0
        ):
            direction = "long"
            clarity = min(candidate_lower_wick_atr / 2.0, 1.0)
            lower_wick_atr = candidate_lower_wick_atr
            upper_wick_atr = candidate_upper_wick_atr
            signal_idx = work.height - recent.height + local_idx
            signal_low = low
            signal_high = high
            signal_vol_ratio = max(signal_vol_ratio, vol_ratio_candidate)
            reclaim_level = prev_low
            break
        if (
            high > prev_high
            and short_reclaim
            and candidate_upper_wick_atr >= min_wick_atr
            and close_position <= 0.52
        ):
            direction = "short"
            clarity = min(candidate_upper_wick_atr / 2.0, 1.0)
            lower_wick_atr = candidate_lower_wick_atr
            upper_wick_atr = candidate_upper_wick_atr
            signal_idx = work.height - recent.height + local_idx
            signal_low = low
            signal_high = high
            signal_vol_ratio = max(signal_vol_ratio, vol_ratio_candidate)
            reclaim_level = prev_high
            break
        if (
            candidate_upper_wick_atr >= min_wick_atr * 1.35
            and close <= bar_close * 1.004
            and close_position <= 0.55
            and rsi >= float(effective_params["min_rsi_short"]) - 15.0
        ):
            direction = "short"
            clarity = min(candidate_upper_wick_atr / 2.0, 1.0)
            lower_wick_atr = candidate_lower_wick_atr
            upper_wick_atr = candidate_upper_wick_atr
            signal_idx = work.height - recent.height + local_idx
            signal_low = low
            signal_high = high
            signal_vol_ratio = max(signal_vol_ratio, vol_ratio_candidate)
            reclaim_level = prev_high
            break

    if direction is None:
        best_idx = -1
        best_vol = 0.0
        for local_idx in range(recent.height - 1, -1, -1):
            candidate_vol = _as_float(recent.item(local_idx, "volume_ratio20"), 1.0)
            if candidate_vol >= best_vol:
                best_vol = candidate_vol
                best_idx = local_idx
        if best_idx >= 0 and best_vol >= adaptive_min_volume:
            open_ = _as_float(recent.item(best_idx, "open"))
            high = _as_float(recent.item(best_idx, "high"))
            low = _as_float(recent.item(best_idx, "low"))
            bar_close = _as_float(recent.item(best_idx, "close"))
            close_position = _as_float(recent.item(best_idx, "close_position"), 0.5)
            prev_low = _as_float(recent.item(best_idx, "prev_donchian_low20"))
            prev_high = _as_float(recent.item(best_idx, "prev_donchian_high20"))
            body = abs(bar_close - open_)
            bar_range = max(high - low, atr)
            last_delta = close - bar_close
            lower_wick_atr = (min(open_, bar_close) - low) / atr
            upper_wick_atr = (high - max(open_, bar_close)) / atr
            body_reversal_long = (
                close_position >= float(effective_params["body_reversal_close_position_long"])
                and bar_close >= open_
                and last_delta >= -atr * 0.20
                and body / bar_range >= 0.20
            )
            body_reversal_short = (
                close_position <= float(effective_params["body_reversal_close_position_short"])
                and bar_close <= open_
                and last_delta <= atr * 0.20
                and body / bar_range >= 0.20
            )
            if (lower_wick_atr >= min_wick_atr or body_reversal_long) and rsi <= 58.0:
                direction = "long"
                clarity = min(max(lower_wick_atr, body / bar_range) / 2.0, 1.0)
                signal_low = low
                signal_high = high
                signal_vol_ratio = max(signal_vol_ratio, best_vol)
                signal_idx = work.height - recent.height + best_idx
                reclaim_level = prev_low
            elif (upper_wick_atr >= min_wick_atr or body_reversal_short) and rsi >= 42.0:
                direction = "short"
                clarity = min(max(upper_wick_atr, body / bar_range) / 2.0, 1.0)
                signal_low = low
                signal_high = high
                signal_vol_ratio = max(signal_vol_ratio, best_vol)
                signal_idx = work.height - recent.height + best_idx
                reclaim_level = prev_high
        if direction is None:
            last_open = _as_float(work.item(-1, "open"))
            last_high = _as_float(work.item(-1, "high"))
            last_low = _as_float(work.item(-1, "low"))
            last_close = _as_float(work.item(-1, "close"))
            last_lower_wick_atr = (min(last_open, last_close) - last_low) / atr
            last_upper_wick_atr = (last_high - max(last_open, last_close)) / atr
            _reject(
                prepared,
                setup_id,
                "no_volume_climax_reclaim",
                lower_wick_atr=last_lower_wick_atr,
                upper_wick_atr=last_upper_wick_atr,
                rsi=rsi,
                best_volume_ratio=best_vol,
            )
            return None

    signal_wick_atr = lower_wick_atr if direction == "long" else upper_wick_atr
    strong_wick = signal_wick_atr >= (min_wick_atr * strong_wick_multiplier)
    required_volume = configured_min_volume
    if strong_wick:
        required_volume = min(configured_min_volume, adaptive_min_volume)
    vol_ratio = signal_vol_ratio
    if vol_ratio < required_volume:
        _reject(
            prepared,
            setup_id,
            "volume_climax_missing",
            volume_ratio=vol_ratio,
            required_volume_ratio=required_volume,
            adaptive_volume=strong_wick,
        )
        return None

    climax_open = _as_float(work.item(signal_idx, "open"))
    climax_close = _as_float(work.item(signal_idx, "close"))
    climax_close_pos = _as_float(work.item(signal_idx, "close_position"), 0.5)
    if direction == "short" and climax_close >= climax_open and climax_close_pos >= 0.55:
        _reject(
            prepared,
            setup_id,
            "volume_climax_continuation_bar",
            close_position=climax_close_pos,
            direction=direction,
        )
        return None
    if direction == "long" and climax_close <= climax_open and climax_close_pos <= 0.45:
        _reject(
            prepared,
            setup_id,
            "volume_climax_continuation_bar",
            close_position=climax_close_pos,
            direction=direction,
        )
        return None

    adx_1h = getattr(prepared, "adx_1h", None)
    if adx_1h is None and not prepared.work_1h.is_empty() and "adx14" in prepared.work_1h.columns:
        adx_1h = _as_float(prepared.work_1h.item(-1, "adx14"), 0.0)
    adx_4h = None
    if not prepared.work_4h.is_empty() and "adx14" in prepared.work_4h.columns:
        adx_4h = _as_float(prepared.work_4h.item(-1, "adx14"), 0.0)
    adx_15m = None
    if not work.is_empty() and "adx14" in work.columns:
        adx_15m = _as_float(work.item(-1, "adx14"), 0.0)
    strong_trend_adx = float(effective_params.get("max_trend_adx", _defaults.get("max_trend_adx", 20.0)))
    if (adx_1h is not None and adx_1h > strong_trend_adx) or (
        adx_4h is not None and adx_4h > strong_trend_adx
    ) or (adx_15m is not None and adx_15m > strong_trend_adx):
        _reject(
            prepared,
            setup_id,
            "volume_climax_strong_trend_adx",
            adx_1h=adx_1h,
            adx_4h=adx_4h,
            adx_15m=adx_15m,
            max_trend_adx=strong_trend_adx,
        )
        return None

    bias_1h = getattr(prepared, "bias_1h", prepared.bias_4h)
    market_regime = str(getattr(prepared, "market_regime", "") or "").lower()
    if market_regime == "trending":
        _reject(
            prepared,
            setup_id,
            "volume_climax_trend_regime_blocked",
            market_regime=market_regime,
            bias_1h=bias_1h,
            direction=direction,
        )
        return None
    sl_buffer = float(effective_params["sl_buffer_atr"])
    min_rr = float(effective_params["min_rr"])
    signal_mid = (signal_high + signal_low) / 2.0
    if direction == "long":
        stop = signal_low - atr * sl_buffer
        price_anchor = reclaim_level if reclaim_level > signal_low else min(signal_mid, close)
        price_anchor = max(price_anchor, stop + atr * 0.10)
        risk = price_anchor - stop
        tp1 = price_anchor + risk * min_rr
        tp2 = price_anchor + risk * max(2.0, min_rr + 0.35)
    else:
        stop = signal_high + atr * sl_buffer
        if 0.0 < reclaim_level < signal_high:
            price_anchor = reclaim_level
        else:
            price_anchor = max(signal_mid, close)
        price_anchor = min(price_anchor, stop - atr * 0.10)
        risk = stop - price_anchor
        tp1 = price_anchor - risk * min_rr
        tp2 = price_anchor - risk * max(2.0, min_rr + 0.35)
    if risk <= 0.0:
        _reject(prepared, setup_id, "invalid_stop", stop=stop, close=price_anchor)
        return None

    base_score = float(effective_params["base_score"])
    score = _compute_dynamic_score(
        direction=direction,
        base_score=base_score,
        vol_ratio=vol_ratio,
        rsi=rsi,
        structure_clarity=clarity,
    )

    # Counter-trend shorts in uptrend require HTF exhaustion evidence; without it,
    # climax shorts against a rising trend have high fail rate (research Q187).
    bias_4h = getattr(prepared, "bias_4h", None)
    if direction == "short" and bias_1h == "uptrend":
        htf_exhaustion = (
            rsi >= float(effective_params.get("htf_exhaustion_rsi_min", 72.0))
            or (bias_4h is not None and bias_4h != "uptrend")
        )
        if not htf_exhaustion:
            _reject(
                prepared,
                setup_id,
                "counter_trend_short_no_htf_exhaustion",
                bias_1h=bias_1h,
                bias_4h=bias_4h,
                rsi=rsi,
            )
            return None

    # Graded bias alignment
    if (direction == "long" and bias_1h == "downtrend") or (
        direction == "short" and bias_1h == "uptrend"
    ):
        score *= effective_params.get("bias_mismatch_penalty", 0.75)

    # RSI extremes graded penalty
    if (direction == "long" and rsi > float(effective_params["max_rsi_long"])) or (
        direction == "short" and rsi < float(effective_params["min_rsi_short"])
    ):
        score *= 0.85

    reasons = [
        f"volume_climax_reversal_{direction}",
        f"vol_ratio={vol_ratio:.2f}",
        f"lower_wick_atr={lower_wick_atr:.2f}",
        f"upper_wick_atr={upper_wick_atr:.2f}",
        f"signal_lag={work.height - 1 - signal_idx}",
        f"reclaim_level={reclaim_level:.4f}",
        f"limit_entry={price_anchor:.4f}",
    ]
    return _build_signal(
        prepared=prepared,
        setup_id=setup_id,
        direction=direction,
        score=score,
        timeframe="15m",
        reasons=reasons,
        strategy_family=family,
        stop=stop,
        tp1=tp1,
        tp2=tp2,
        price_anchor=price_anchor,
        atr=atr,
    )


def detect_volume_climax_reversal_setup(
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
        spec_detect=detect_volume_climax_reversal,
        extended_detect=_detect_volume_climax_reversal_extended,
        spec_kwargs=spec_kwargs,
    )


__all__ = [
    "_detect_volume_climax_reversal_extended",
    "detect_volume_climax_reversal",
    "detect_volume_climax_reversal_setup",
]


class VolumeClimaxReversalSetup(SpecDetectorSetup):
    setup_id = "volume_climax_reversal"
    ENTRY_ORDER_TYPE: ClassVar[str] = "market"
    family = "reversal"
    confirmation_profile = "countertrend_exhaustion"
    required_context = ("futures_flow",)

    DEFAULTS: ClassVar[dict[str, float]] = {
        "base_score": 0.60,
        "min_volume_ratio": 2.0,
        "adaptive_min_volume_ratio": 1.5,
        "min_wick_atr": 0.45,
        "strong_wick_multiplier": 1.35,
        "signal_lookback_bars": 10,
        "body_reversal_close_position_long": 0.62,
        "body_reversal_close_position_short": 0.38,
        "max_rsi_long": 42.0,
        "min_rsi_short": 58.0,
        "sl_buffer_atr": 0.45,
        "min_rr": 1.9,
        "max_trend_adx": 20.0,
        "htf_exhaustion_rsi_min": 72.0,
    }

    detect_setup = detect_volume_climax_reversal_setup


__all__ = ["VolumeClimaxReversalSetup"]
