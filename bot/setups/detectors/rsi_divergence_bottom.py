"""rsi_divergence_bottom detector."""
from __future__ import annotations

from ...domain.config import BotSettings
from ...domain.schemas import PreparedSymbol, Signal
from ._roadmap import _as_float, _build_atr_signal, _missing_columns, _reject
from ._common import build_spec_signal
from ...domain.strategy_catalog import catalog_default_params
from .indicator_divergence import detect_regular_divergence

__all__ = ["detect_rsi_divergence_bottom"]


def detect_rsi_divergence_bottom(
    prepared: PreparedSymbol,
    settings: BotSettings,
    effective_params: dict[str, float],
    *,
    setup_id: str,
    family: str,
) -> Signal | None:
    params = effective_params
    work = prepared.work_15m
    hit = detect_regular_divergence(work, timeframe="15m", require_oversold=True)
    if hit is not None:
        return build_spec_signal(
            prepared=prepared,
            settings=settings,
            setup_id=setup_id,
            family=family,
            hit=hit,
            defaults=catalog_default_params(setup_id),
            params=params,
        )

    # FIX 2026-05-21: strict RSI spec can miss valid windowed divergence;
    # fall through to the existing configured detector before rejecting.
    missing = _missing_columns(work, ("open", "high", "low", "close", "atr14", "rsi14"))
    if missing:
        _reject(prepared, setup_id, "missing_columns", missing_fields=missing)
        return None
    window = int(params["divergence_window"])
    if work.height < window * 2:
        _reject(prepared, setup_id, "insufficient_divergence_window")
        return None
    previous = work.slice(work.height - window * 2, window)
    recent = work.tail(window)
    previous_lows = [_as_float(value) for value in previous["low"].to_list()]
    recent_lows = [_as_float(value) for value in recent["low"].to_list()]
    previous_highs = [_as_float(value) for value in previous["high"].to_list()]
    recent_highs = [_as_float(value) for value in recent["high"].to_list()]
    if not previous_lows or not recent_lows or not previous_highs or not recent_highs:
        _reject(prepared, setup_id, "rsi_divergence_missing")
        return None
    prev_low_idx = min(range(len(previous_lows)), key=previous_lows.__getitem__)
    recent_low_idx = min(range(len(recent_lows)), key=recent_lows.__getitem__)
    prev_high_idx = max(range(len(previous_highs)), key=previous_highs.__getitem__)
    recent_high_idx = max(range(len(recent_highs)), key=recent_highs.__getitem__)
    prev_low = previous_lows[prev_low_idx]
    recent_low = recent_lows[recent_low_idx]
    prev_high = previous_highs[prev_high_idx]
    recent_high = recent_highs[recent_high_idx]
    prev_rsi_low = _as_float(previous.item(prev_low_idx, "rsi14"), 50.0)
    recent_rsi_low = _as_float(recent.item(recent_low_idx, "rsi14"), 50.0)
    prev_rsi_high = _as_float(previous.item(prev_high_idx, "rsi14"), 50.0)
    recent_rsi_high = _as_float(recent.item(recent_high_idx, "rsi14"), 50.0)
    price_delta = float(params["min_price_delta_pct"]) / 100.0
    retest_delta = float(params.get("near_retest_pct", 0.18)) / 100.0
    rsi_delta = float(params["min_rsi_delta"])
    current_close = _as_float(work.item(-1, "close"))
    current_open = _as_float(work.item(-1, "open"))
    current_atr = _as_float(work.item(-1, "atr14"))
    current_rsi = _as_float(work.item(-1, "rsi14"), 50.0)
    close_position = (
        _as_float(work.item(-1, "close_position"), 0.5)
        if "close_position" in work.columns
        else 0.5
    )
    entry_anchor: float | None = None
    score_penalty = 1.0
    reason_prefix = "rsi_divergence"
    if (
        recent_low < prev_low * (1.0 - price_delta)
        and recent_rsi_low >= prev_rsi_low + rsi_delta
    ):
        direction = "long"
    elif (
        recent_high > prev_high * (1.0 + price_delta)
        and recent_rsi_high <= prev_rsi_high - rsi_delta
    ):
        direction = "short"
    else:
        direction = None

    if direction is None and current_atr > 0.0 and current_close > 0.0:
        recovery_delta = float(params.get("min_recovery_rsi_delta", 3.0))
        max_long_rsi = float(params.get("max_long_rsi", 52.0))
        min_short_rsi = float(params.get("min_short_rsi", 48.0))
        long_close_position = float(params.get("min_reversal_close_position_long", 0.50))
        short_close_position = float(params.get("max_reversal_close_position_short", 0.50))
        recent_low_near_previous = recent_low <= prev_low * (1.0 + retest_delta)
        recent_high_near_previous = recent_high >= prev_high * (1.0 - retest_delta)
        current_reclaimed_low = current_close >= recent_low + current_atr * 0.20
        current_rejected_high = current_close <= recent_high - current_atr * 0.20
        low_rsi_recovered = current_rsi >= recent_rsi_low + recovery_delta
        high_rsi_rejected = current_rsi <= recent_rsi_high - recovery_delta
        bullish_reversal_bar = current_close >= current_open or close_position >= long_close_position
        bearish_reversal_bar = current_close <= current_open or close_position <= short_close_position
        if (
            recent_low_near_previous
            and (
                recent_rsi_low >= prev_rsi_low + rsi_delta * 0.5
                or low_rsi_recovered
            )
            and current_reclaimed_low
            and current_rsi <= max_long_rsi
            and bullish_reversal_bar
        ):
            direction = "long"
            entry_anchor = max(recent_low + current_atr * 0.25, min(current_close, prev_low))
            score_penalty = (
                float(params.get("adaptive_retest_penalty", 0.90))
                if recent_rsi_low < prev_rsi_low + rsi_delta
                else 1.0
            )
            reason_prefix = "rsi_retest_recovery"
        elif (
            recent_high_near_previous
            and (
                recent_rsi_high <= prev_rsi_high - rsi_delta * 0.5
                or high_rsi_rejected
            )
            and current_rejected_high
            and current_rsi >= min_short_rsi
            and bearish_reversal_bar
        ):
            direction = "short"
            entry_anchor = min(recent_high - current_atr * 0.25, max(current_close, prev_high))
            score_penalty = (
                float(params.get("adaptive_retest_penalty", 0.90))
                if recent_rsi_high > prev_rsi_high - rsi_delta
                else 1.0
            )
            reason_prefix = "rsi_retest_rejection"

    if direction is None and current_atr > 0.0:
        scan = work.tail(min(max(window * 3, 36), work.height))
        lows = [_as_float(value) for value in scan["low"].to_list()]
        highs = [_as_float(value) for value in scan["high"].to_list()]
        rsis = [_as_float(value, 50.0) for value in scan["rsi14"].to_list()]
        if lows and highs and rsis:
            min_recent_rsi = min(rsis[-window:])
            max_recent_rsi = max(rsis[-window:])
            low_idx = min(range(len(lows)), key=lows.__getitem__)
            high_idx = max(range(len(highs)), key=highs.__getitem__)
            current_low_rank = (current_close - min(lows)) / max(max(highs) - min(lows), 1e-9)
            current_high_rank = (max(highs) - current_close) / max(max(highs) - min(lows), 1e-9)
            if (
                low_idx >= len(lows) - window
                and min_recent_rsi <= 42.0
                and current_rsi >= min_recent_rsi + float(params.get("min_recovery_rsi_delta", 3.0))
                and current_low_rank <= 0.35
                and close_position >= float(params.get("min_reversal_close_position_long", 0.50))
            ):
                direction = "long"
                entry_anchor = max(lows[low_idx] + current_atr * 0.30, current_close - current_atr * 0.20)
                score_penalty = float(params.get("rsi_recovery_penalty", 0.86))
                reason_prefix = "rsi_bottom_recovery"
                recent_low = lows[low_idx]
                recent_rsi_low = rsis[low_idx]
            elif (
                high_idx >= len(highs) - window
                and max_recent_rsi >= 58.0
                and current_rsi <= max_recent_rsi - float(params.get("min_recovery_rsi_delta", 3.0))
                and current_high_rank <= 0.35
                and close_position <= float(params.get("max_reversal_close_position_short", 0.50))
            ):
                direction = "short"
                entry_anchor = min(highs[high_idx] - current_atr * 0.30, current_close + current_atr * 0.20)
                score_penalty = float(params.get("rsi_recovery_penalty", 0.86))
                reason_prefix = "rsi_top_rejection"
                recent_high = highs[high_idx]
                recent_rsi_high = rsis[high_idx]

    if direction is None:
        _reject(
            prepared,
            setup_id,
            "rsi_divergence_missing",
            prev_low=prev_low,
            recent_low=recent_low,
            prev_rsi_low=prev_rsi_low,
            recent_rsi_low=recent_rsi_low,
            prev_high=prev_high,
            recent_high=recent_high,
            prev_rsi_high=prev_rsi_high,
            recent_rsi_high=recent_rsi_high,
            current_rsi=current_rsi,
        )
        return None
    structure_clarity = 0.7 * score_penalty
    return _build_atr_signal(
        prepared=prepared,
        setup_id=setup_id,
        direction=direction,
        params=params,
        reasons=[
            f"{reason_prefix}_{direction}",
            f"price_low={prev_low:.4f}->{recent_low:.4f}",
            f"price_high={prev_high:.4f}->{recent_high:.4f}",
            (
                f"rsi_low={prev_rsi_low:.1f}->{recent_rsi_low:.1f} "
                f"rsi_high={prev_rsi_high:.1f}->{recent_rsi_high:.1f}"
            ),
            f"current_rsi={current_rsi:.1f}",
            f"close_position={close_position:.2f}",
            f"score_penalty={score_penalty:.2f}",
        ],
        family=family,
        structure_clarity=structure_clarity,
        entry_anchor=entry_anchor,
    )
