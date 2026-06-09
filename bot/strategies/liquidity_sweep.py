"""liquidity_sweep - canonical strategy detector."""

from __future__ import annotations

import logging
import math
from typing import TYPE_CHECKING, ClassVar

import polars as pl

from ..domain.strategy_catalog import catalog_default_params
from ..features.prepare import _swing_points
from ..setups import _build_signal, _compute_dynamic_score, _reject
from ..setups.smc import latest_liquidity_sweep, sweep_tolerance, swing_series
from ..setups.spec_runtime import SpecDetectorSetup, run_setup_detection
from ..setups.utils import build_smc_trade_plan
from ._common import (
    SpecHit,
    _latest_values,
    build_spec_signal,
    confirmed_pattern_frame,
    orderflow_supports_reversal,
    with_spec_columns,
)
from ._roadmap import _build_atr_signal, _last, _missing_columns, _prev

if TYPE_CHECKING:
    from ..domain.config import BotSettings
    from ..domain.schemas import PreparedSymbol, Signal

LOG = logging.getLogger("bot.strategies.liquidity_sweep")

__all__ = ["detect_liquidity_sweep"]


def detect_liquidity_sweep(
    frame: pl.DataFrame,
    *,
    timeframe: str = "15m",
    sweep_atr_mult: float = 0.2,
) -> SpecHit | None:
    work = with_spec_columns(frame)
    if work.height < 25:
        return None
    row = _latest_values(work)
    atr = row.get("spec_atr14", 0.0)
    prev_high = row.get("spec_prev_high20", 0.0)
    prev_low = row.get("spec_prev_low20", 0.0)
    close = row.get("close", 0.0)
    high = row.get("high", 0.0)
    low = row.get("low", 0.0)
    vol_ratio = row.get("volume_ratio20", 1.0)
    rsi = row.get("rsi14", 50.0)
    if atr <= 0.0:
        return None
    high_tol = sweep_tolerance(level=prev_high, atr=atr, sweep_atr_mult=sweep_atr_mult)
    low_tol = sweep_tolerance(level=prev_low, atr=atr, sweep_atr_mult=sweep_atr_mult)
    if high > prev_high + high_tol and close < prev_high and (high - close) > high_tol:
        return SpecHit(
            strategy="liquidity_sweep",
            direction="short",
            entry=prev_high,
            stop_basis=high,
            atr=atr,
            timeframe=timeframe,
            reasons=(f"sweep_high level={prev_high:.4f}", f"wick_atr={(high - close) / atr:.2f}"),
            vol_ratio=vol_ratio,
            rsi=rsi,
        )
    if low < prev_low - low_tol and close > prev_low and (close - low) > low_tol:
        return SpecHit(
            strategy="liquidity_sweep",
            direction="long",
            entry=prev_low,
            stop_basis=low,
            atr=atr,
            timeframe=timeframe,
            reasons=(f"sweep_low level={prev_low:.4f}", f"wick_atr={(close - low) / atr:.2f}"),
            vol_ratio=vol_ratio,
            rsi=rsi,
        )
    return None


_SCAN_BARS = 30
_EQUAL_TOL = 0.0015  # 0.15%


def _as_float(value: object, default: float = 0.0) -> float:
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    return default


def _detect_liquidity_sweep_extended(
    prepared: PreparedSymbol,
    _settings: BotSettings,
    defaults: dict[str, float],
    effective: dict[str, float],
    setup_id: str,
    family: str,
) -> Signal | None:
    dynamic_params = effective
    equal_level_tol = float(
        dynamic_params.get(
            "equal_level_tol",
            dynamic_params.get("threshold_tol", defaults["equal_level_tol"]),
        )
    )
    min_level_hits = max(2, int(dynamic_params.get("min_level_hits", defaults["min_level_hits"])))
    sweep_atr_mult = float(dynamic_params.get("sweep_atr_mult", defaults["sweep_atr_mult"]))
    reclaim_threshold = float(
        dynamic_params.get("reclaim_threshold", defaults["reclaim_threshold"])
    )
    sl_buffer_atr = float(dynamic_params.get("sl_buffer_atr", defaults["sl_buffer_atr"]))
    min_rr = float(dynamic_params.get("min_rr", defaults["min_rr"]))
    base_score = float(dynamic_params.get("base_score", defaults["base_score"]))
    max_sweep_age_bars = int(
        dynamic_params.get("max_sweep_age_bars", defaults["max_sweep_age_bars"])
    )
    max_entry_distance_atr = float(
        dynamic_params.get("max_entry_distance_atr", defaults["max_entry_distance_atr"])
    )
    w = confirmed_pattern_frame(prepared.work_1h)
    w15m = confirmed_pattern_frame(prepared.work_15m)
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
    # EQH/EQL tolerance: max(fixed %, ~0.1×ATR) per deep-research-report-4 §A2.
    atr_tol_pct = (0.1 * atr / price) if price > 0.0 else 0.0
    range_percent = max(equal_level_tol, atr_tol_pct)
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
        range_percent=range_percent,
    )
    fallback_direction: str | None = None
    fallback_level: float | None = None
    fallback_sweep_index: int | None = None
    fallback_state = ""
    if zone is None or zone.sweep_index is None or zone.state == "invalidated":
        if {"prev_donchian_low20", "prev_donchian_high20"}.issubset(set(scan.columns)):
            first_idx = max(0, scan.height - max_sweep_age_bars - 1)
            for idx in range(scan.height - 1, first_idx - 1, -1):
                prev_low = _as_float(scan.item(idx, "prev_donchian_low20"))
                prev_high = _as_float(scan.item(idx, "prev_donchian_high20"))
                bar_high = _as_float(scan.item(idx, "high"))
                bar_low = _as_float(scan.item(idx, "low"))
                bar_close = _as_float(scan.item(idx, "close"))
                if min(prev_low, prev_high, bar_high, bar_low, bar_close) <= 0.0:
                    continue
                pierce = sweep_tolerance(
                    level=prev_high,
                    atr=atr,
                    sweep_atr_mult=sweep_atr_mult,
                    tolerance_pct=equal_level_tol,
                )
                if bar_high > prev_high + pierce and bar_close < prev_high:
                    fallback_direction = "short"
                    fallback_level = prev_high
                    fallback_sweep_index = idx
                    fallback_state = "donchian_fallback"
                    break
                pierce = sweep_tolerance(
                    level=prev_low,
                    atr=atr,
                    sweep_atr_mult=sweep_atr_mult,
                    tolerance_pct=equal_level_tol,
                )
                if bar_low < prev_low - pierce and bar_close > prev_low:
                    fallback_direction = "long"
                    fallback_level = prev_low
                    fallback_sweep_index = idx
                    fallback_state = "donchian_fallback"
                    break
        if fallback_direction is None or fallback_level is None or fallback_sweep_index is None:
            _reject(prepared, setup_id, "no_liquidity_sweep_detected")
            return None

    direction = (
        zone.direction if zone is not None and zone.sweep_index is not None else fallback_direction
    )
    level = (
        (zone.level or zone.midpoint)
        if zone is not None and zone.sweep_index is not None
        else fallback_level
    )
    zone_state = zone.state if zone is not None and zone.sweep_index is not None else fallback_state
    sweep_index = int(
        zone.sweep_index
        if zone is not None and zone.sweep_index is not None
        else fallback_sweep_index
    )
    if direction not in {"long", "short"} or level is None:
        _reject(prepared, setup_id, "invalid_liquidity_sweep_state")
        return None
    if not (0 <= sweep_index < n):
        _reject(
            prepared,
            setup_id,
            "liquidity_sweep_index_out_of_bounds",
            sweep_index=sweep_index,
            bars=n,
        )
        return None
    sweep_age = scan.height - 1 - sweep_index
    if sweep_age > max_sweep_age_bars:
        _reject(
            prepared,
            setup_id,
            "liquidity_sweep_too_old",
            sweep_age=sweep_age,
            max_sweep_age_bars=max_sweep_age_bars,
        )
        return None
    sweep_bar_h = float(highs[sweep_index])
    sweep_bar_l = float(lows[sweep_index])
    sweep_bar_c = float(closes[sweep_index])
    min_wick_pen = sweep_atr_mult * atr
    level_f = float(level)
    if direction == "short":
        if sweep_bar_h < level_f + min_wick_pen:
            _reject(
                prepared,
                setup_id,
                "liquidity_sweep_wick_too_shallow",
                direction="short",
                wick_pen_atr=(sweep_bar_h - level_f) / atr if atr > 0 else 0.0,
                min_sweep_atr_mult=sweep_atr_mult,
            )
            return None
    else:
        if sweep_bar_l > level_f - min_wick_pen:
            _reject(
                prepared,
                setup_id,
                "liquidity_sweep_wick_too_shallow",
                direction="long",
                wick_pen_atr=(level_f - sweep_bar_l) / atr if atr > 0 else 0.0,
                min_sweep_atr_mult=sweep_atr_mult,
            )
            return None
    zone_low = sweep_bar_l
    zone_high = sweep_bar_h
    entry_price = (zone_low + zone_high) / 2.0
    sweep_pad_atr = (zone_high - zone_low) / (2.0 * atr) if atr > 0.0 else 0.35
    confirmation_close = float(closes[-1])
    if not w15m.is_empty() and "close" in w15m.columns and w15m.height >= 1:
        confirmation_close = _as_float(w15m.item(-1, "close"), confirmation_close)

    level_f = float(level)
    reclaim_timeout_bars = int(
        dynamic_params.get("reclaim_timeout_bars", defaults.get("reclaim_timeout_bars", 4))
    )
    if direction == "short":
        reclaim_ok = (
            sweep_bar_h > level_f
            and sweep_bar_c < level_f
            and confirmation_close < level_f + reclaim_threshold * atr
        )
    else:
        reclaim_ok = (
            sweep_bar_l < level_f
            and sweep_bar_c > level_f
            and confirmation_close > level_f - reclaim_threshold * atr
        )
    if sweep_age >= reclaim_timeout_bars and not reclaim_ok:
        _reject(
            prepared,
            setup_id,
            "liquidity_sweep_reclaim_timeout",
            sweep_age=sweep_age,
            reclaim_timeout_bars=reclaim_timeout_bars,
        )
        return None

    if direction == "short":
        eq_high_level = level
        if eq_high_level is None or not math.isfinite(float(eq_high_level)):
            _reject(prepared, setup_id, "liquidity_level_missing", direction="short")
            return None
        if (
            sweep_bar_h <= eq_high_level
            or sweep_bar_c >= eq_high_level
            or confirmation_close >= eq_high_level + reclaim_threshold * atr
        ):
            _reject(
                prepared,
                setup_id,
                "short_reclaim_not_confirmed",
                level=eq_high_level,
            )
            return None
        if abs(entry_price - confirmation_close) > max_entry_distance_atr * atr:
            _reject(
                prepared,
                setup_id,
                "entry_too_far_from_confirmation",
                price=entry_price,
                close=confirmation_close,
                max_entry_distance_atr=max_entry_distance_atr,
            )
            return None

        pivots = swing_series(w, swing_length=3, include_unconfirmed_tail=True)
        trade_plan = build_smc_trade_plan(
            direction="short",
            price_anchor=entry_price,
            stop_basis=sweep_bar_h,
            atr=atr,
            work_1h=w,
            work_4h=prepared.work_4h,
            min_rr=min_rr,
            sl_buffer_atr=sl_buffer_atr,
            sh_mask=None,
            sl_mask=pivots.low_mask,
        )
        if trade_plan is None:
            _reject(
                prepared,
                setup_id,
                "risk_non_positive_short",
                stop_basis=sweep_bar_h,
                price=entry_price,
            )
            return None
        stop = trade_plan.stop
        tp1 = trade_plan.tp1
        tp2 = trade_plan.tp2
        risk = trade_plan.risk
        if tp1 >= entry_price or abs(tp1 - entry_price) + 1e-9 < risk * min_rr:
            _reject(
                prepared,
                setup_id,
                "tp1_too_close_or_missing",
                tp1=tp1,
                risk=risk,
                price=entry_price,
            )
            return None

        vol_ratio = _as_float(w.item(-1, "volume_ratio20"), 1.0)
        rsi = _as_float(w.item(-1, "rsi14"), 50.0)
        score = _compute_dynamic_score(
            direction="short",
            base_score=base_score,
            vol_ratio=vol_ratio,
            rsi=rsi,
        )
        reasons = [
            f"Liquidity sweep short: eq_high={eq_high_level:.4f} state={zone_state}",
            (
                f"wick={sweep_bar_h:.4f} close={sweep_bar_c:.4f} "
                f"confirm={confirmation_close:.4f} age={sweep_age}"
            ),
        ]
        return _build_signal(
            prepared=prepared,
            setup_id=setup_id,
            direction="short",
            score=score,
            timeframe="1h",
            reasons=reasons,
            strategy_family=family,
            stop=stop,
            tp1=tp1,
            tp2=tp2,
            price_anchor=entry_price,
            atr=atr,
            entry_pad_atr_mult=sweep_pad_atr,
        )

    eq_low_level = level
    if eq_low_level is None or not math.isfinite(float(eq_low_level)):
        _reject(prepared, setup_id, "liquidity_level_missing", direction="long")
        return None
    if (
        sweep_bar_l >= eq_low_level
        or sweep_bar_c <= eq_low_level
        or confirmation_close <= eq_low_level - reclaim_threshold * atr
    ):
        _reject(prepared, setup_id, "long_reclaim_not_confirmed", level=eq_low_level)
        return None
    if abs(entry_price - confirmation_close) > max_entry_distance_atr * atr:
        _reject(
            prepared,
            setup_id,
            "entry_too_far_from_confirmation",
            price=entry_price,
            close=confirmation_close,
            max_entry_distance_atr=max_entry_distance_atr,
        )
        return None

    pivots = swing_series(w, swing_length=3, include_unconfirmed_tail=True)
    trade_plan = build_smc_trade_plan(
        direction="long",
        price_anchor=entry_price,
        stop_basis=sweep_bar_l,
        atr=atr,
        work_1h=w,
        work_4h=prepared.work_4h,
        min_rr=min_rr,
        sl_buffer_atr=sl_buffer_atr,
        sh_mask=pivots.high_mask,
        sl_mask=None,
    )
    if trade_plan is None:
        _reject(
            prepared, setup_id, "risk_non_positive_long", stop_basis=sweep_bar_l, price=entry_price
        )
        return None
    stop = trade_plan.stop
    tp1 = trade_plan.tp1
    tp2 = trade_plan.tp2
    risk = trade_plan.risk
    if tp1 <= entry_price or abs(tp1 - entry_price) + 1e-9 < risk * min_rr:
        _reject(
            prepared,
            setup_id,
            "tp1_too_close_or_missing",
            tp1=tp1,
            risk=risk,
            price=entry_price,
        )
        return None

    vol_ratio = _as_float(w.item(-1, "volume_ratio20"), 1.0)
    rsi = _as_float(w.item(-1, "rsi14"), 50.0)
    score = _compute_dynamic_score(
        direction="long",
        base_score=base_score,
        vol_ratio=vol_ratio,
        rsi=rsi,
    )
    reasons = [
        f"Liquidity sweep long: eq_low={eq_low_level:.4f} state={zone_state}",
        (
            f"wick={sweep_bar_l:.4f} close={sweep_bar_c:.4f} "
            f"confirm={confirmation_close:.4f} age={sweep_age}"
        ),
    ]
    return _build_signal(
        prepared=prepared,
        setup_id=setup_id,
        direction="long",
        score=score,
        timeframe="1h",
        reasons=reasons,
        strategy_family=family,
        stop=stop,
        tp1=tp1,
        tp2=tp2,
        price_anchor=entry_price,
        atr=atr,
        entry_pad_atr_mult=sweep_pad_atr,
    )


def _detect_fakeout_reclaim(
    prepared: PreparedSymbol,
    _settings: BotSettings,
    effective: dict[str, float],
    *,
    setup_id: str,
    family: str,
) -> Signal | None:
    """Fakeout reclaim without structure (merged from fakeout_detector)."""
    min_volume_ratio = _as_float(effective.get("min_volume_ratio", 1.0), 1.0)
    fakeout_lookback = max(5, int(_as_float(effective.get("fakeout_lookback_bars", 20), 20)))
    fakeout_window = max(1, int(_as_float(effective.get("fakeout_window_bars", 3), 3)))

    w = confirmed_pattern_frame(prepared.work_15m)
    if w.height < 30:
        _reject(prepared, setup_id, "insufficient_15m_bars", bars=w.height)
        return None

    atr = _last(w, "atr14")
    if atr <= 0:
        _reject(prepared, setup_id, "atr_invalid", atr=atr)
        return None

    lookback_tail = w.tail(min(fakeout_lookback, w.height))
    sh_mask, sl_mask = _swing_points(lookback_tail, n=2, include_unconfirmed_tail=True)
    swing_highs = lookback_tail.filter(sh_mask)["high"]
    swing_lows = lookback_tail.filter(sl_mask)["low"]
    tail = w.tail(min(fakeout_window + 1, w.height))

    direction = None
    level = 0.0
    sweep_extreme = 0.0
    breakout_idx = -1
    reasons_list: list[str] = []

    for sh_cell in swing_highs:
        sh = _as_float(sh_cell)
        if sh <= 0:
            continue
        for row_idx in range(tail.height):
            bar_high = _as_float(tail.item(row_idx, "high"))
            bar_close = _as_float(tail.item(row_idx, "close"))
            if bar_high > sh and bar_close < sh:
                direction = "short"
                level = sh
                sweep_extreme = bar_high
                breakout_idx = row_idx
                reasons_list.append(f"short_fakeout_swing_high={sh:.4f}")
                break
        if direction is not None:
            break

    if direction is None:
        for sl_cell in swing_lows:
            sl = _as_float(sl_cell)
            if sl <= 0:
                continue
            for row_idx in range(tail.height):
                bar_low = _as_float(tail.item(row_idx, "low"))
                bar_close = _as_float(tail.item(row_idx, "close"))
                if bar_low < sl and bar_close > sl:
                    direction = "long"
                    level = sl
                    sweep_extreme = bar_low
                    breakout_idx = row_idx
                    reasons_list.append(f"long_fakeout_swing_low={sl:.4f}")
                    break
            if direction is not None:
                break

    if direction is None:
        _reject(prepared, setup_id, "fakeout_pattern_missing")
        return None

    breakout_vol = 0.0
    if breakout_idx >= 0 and "volume_ratio20" in tail.columns:
        breakout_vol = _as_float(tail.item(breakout_idx, "volume_ratio20"), 0.0)
        if breakout_vol < min_volume_ratio:
            _reject(
                prepared,
                setup_id,
                "breakout_volume_too_low",
                breakout_vol=breakout_vol,
                min_volume_ratio=min_volume_ratio,
            )
            return None

    clarity = _as_float(effective.get("structure_clarity", 0.5), 0.5)
    if direction == "long":
        proximity = abs(level - sweep_extreme) / atr if atr > 0 else 1.0
    else:
        proximity = abs(sweep_extreme - level) / atr if atr > 0 else 1.0
    if 0 < proximity < 0.5:
        clarity = min(1.0, clarity + (0.5 - proximity) / 0.5 * 0.15)
        reasons_list.append(f"proximity={proximity:.3f}")

    return _build_atr_signal(
        prepared=prepared,
        setup_id=setup_id,
        direction=direction,
        params=effective,
        confirmed_bar=True,
        reasons=[
            f"fakeout_{direction}",
            f"vol_ratio={breakout_vol:.2f}",
            f"level={level:.4f}",
            *reasons_list,
        ],
        family=family,
        structure_clarity=clarity,
        entry_anchor=level if level > 0.0 else None,
        stop_anchor=sweep_extreme if sweep_extreme > 0.0 else None,
    )


def _detect_stop_hunt_spec(frame: pl.DataFrame, *, timeframe: str = "15m") -> SpecHit | None:
    """Stop-cluster sweep (merged from stop_hunt_detection)."""
    work = with_spec_columns(frame)
    if work.height < 20:
        return None
    row = _latest_values(work)
    atr = row.get("spec_atr14", 0.0)
    if atr <= 0.0:
        return None
    recent = work.tail(10)
    high = row["high"]
    low = row["low"]
    close = row["close"]
    prev_high = row.get("spec_prev_high20", 0.0)
    prev_low = row.get("spec_prev_low20", 0.0)
    upper_wick = row.get("spec_upper_wick", 0.0)
    lower_wick = row.get("spec_lower_wick", 0.0)
    upper_ratio = row.get("spec_upper_wick_ratio", 0.0)
    lower_ratio = row.get("spec_lower_wick_ratio", 0.0)
    high_touches = recent.filter((pl.col("high") - prev_high).abs() <= atr * 0.35).height
    low_touches = recent.filter((pl.col("low") - prev_low).abs() <= atr * 0.35).height
    if (
        high > prev_high
        and close < prev_high
        and upper_ratio > 1.35
        and upper_wick > atr * 0.35
        and high_touches >= 1
    ):
        return SpecHit(
            strategy="liquidity_sweep",
            direction="short",
            entry=prev_high,
            stop_basis=high,
            atr=atr,
            timeframe=timeframe,
            reasons=(f"stop_cluster_high={prev_high:.4f}", f"touches={high_touches}"),
            vol_ratio=row.get("volume_ratio20", 1.0),
            rsi=row.get("rsi14", 50.0),
        )
    if (
        low < prev_low
        and close > prev_low
        and lower_ratio > 1.35
        and lower_wick > atr * 0.35
        and low_touches >= 1
    ):
        return SpecHit(
            strategy="liquidity_sweep",
            direction="long",
            entry=prev_low,
            stop_basis=low,
            atr=atr,
            timeframe=timeframe,
            reasons=(f"stop_cluster_low={prev_low:.4f}", f"touches={low_touches}"),
            vol_ratio=row.get("volume_ratio20", 1.0),
            rsi=row.get("rsi14", 50.0),
        )
    return None


def _detect_stop_hunt_fallback(
    prepared: PreparedSymbol,
    settings: BotSettings,
    params: dict[str, float],
    *,
    setup_id: str,
    family: str,
) -> Signal | None:
    """Donchian stop-hunt reclaim path (merged from stop_hunt_detection)."""
    work = confirmed_pattern_frame(prepared.work_15m)
    hit = _detect_stop_hunt_spec(work, timeframe="15m")
    if hit is not None:
        return build_spec_signal(
            prepared=prepared,
            _settings=settings,
            setup_id=setup_id,
            family=family,
            hit=hit,
            defaults=catalog_default_params(setup_id),
            params=params,
        )

    missing = _missing_columns(
        work,
        ("high", "low", "close", "prev_donchian_low20", "prev_donchian_high20"),
    )
    if missing:
        _reject(prepared, setup_id, "missing_columns", missing_fields=missing)
        return None
    if work.height < 2:
        _reject(prepared, setup_id, "insufficient_bars")
        return None
    close = _prev(work, "close")
    atr = _prev(work, "atr14")
    tolerance = max(0.0003, min(float(params.get("sweep_tolerance_pct", 0.0010)), 0.0012))
    min_volume_ratio = max(float(params.get("min_volume_ratio", 0.65)), 0.55)
    min_close_position_long = max(float(params.get("min_close_position_long", 0.55)), 0.55)
    max_close_position_short = min(float(params.get("max_close_position_short", 0.45)), 0.45)
    signal_lookback_bars = max(5, min(int(params.get("signal_lookback_bars", 20)), 24))
    max_entry_drift_atr = max(0.50, min(float(params.get("max_entry_drift_atr", 1.25)), 2.0))
    recent = work.tail(min(signal_lookback_bars, work.height))
    direction = None
    level = 0.0
    signal_lag = 0
    sweep_extreme = 0.0
    vol_ratio = _prev(work, "volume_ratio20", 1.0)
    entry_drift_atr = 0.0
    reclaim_quality = 1.0
    for local_idx in range(recent.height - 1, -1, -1):
        high = _as_float(recent.item(local_idx, "high"))
        low = _as_float(recent.item(local_idx, "low"))
        bar_close = _as_float(recent.item(local_idx, "close"))
        prev_low = _as_float(recent.item(local_idx, "prev_donchian_low20"))
        prev_high = _as_float(recent.item(local_idx, "prev_donchian_high20"))
        close_position = _as_float(recent.item(local_idx, "close_position"), 0.5)
        bar_vol_ratio = _as_float(recent.item(local_idx, "volume_ratio20"), 1.0)
        volume_ok = max(bar_vol_ratio, vol_ratio) >= min_volume_ratio
        long_drift_atr = (close - prev_low) / atr if atr > 0.0 and prev_low > 0.0 else 0.0
        short_drift_atr = (prev_high - close) / atr if atr > 0.0 and prev_high > 0.0 else 0.0
        if (
            prev_low > 0.0
            and low < prev_low * (1.0 - tolerance)
            and max(bar_close, close) > prev_low
            and close_position >= min_close_position_long
            and 0.0 <= long_drift_atr <= max_entry_drift_atr
            and volume_ok
        ):
            direction = "long"
            level = prev_low
            sweep_extreme = low
            entry_drift_atr = long_drift_atr
        elif (
            prev_high > 0.0
            and high > prev_high * (1.0 + tolerance)
            and min(bar_close, close) < prev_high
            and close_position <= max_close_position_short
            and 0.0 <= short_drift_atr <= max_entry_drift_atr
            and volume_ok
        ):
            direction = "short"
            level = prev_high
            sweep_extreme = high
            entry_drift_atr = short_drift_atr
        if direction is not None:
            signal_lag = recent.height - 1 - local_idx
            vol_ratio = max(vol_ratio, bar_vol_ratio)
            break
    if direction is None:
        if atr > 0.0 and recent.height > 0:
            near_level_atr = max(0.10, min(float(params.get("near_level_atr", 0.35)), 0.50))
            min_wick_atr = max(0.25, min(float(params.get("min_wick_atr", 0.35)), 0.75))
            for local_idx in range(recent.height - 1, -1, -1):
                open_ = _as_float(recent.item(local_idx, "open"))
                high = _as_float(recent.item(local_idx, "high"))
                low = _as_float(recent.item(local_idx, "low"))
                bar_close = _as_float(recent.item(local_idx, "close"))
                prev_low = _as_float(recent.item(local_idx, "prev_donchian_low20"))
                prev_high = _as_float(recent.item(local_idx, "prev_donchian_high20"))
                close_position = _as_float(recent.item(local_idx, "close_position"), 0.5)
                bar_vol_ratio = _as_float(recent.item(local_idx, "volume_ratio20"), 1.0)
                lower_wick_atr = (min(open_, bar_close) - low) / atr
                upper_wick_atr = (high - max(open_, bar_close)) / atr
                long_drift_atr = (close - prev_low) / atr if prev_low > 0.0 else 0.0
                short_drift_atr = (prev_high - close) / atr if prev_high > 0.0 else 0.0
                if (
                    prev_low > 0.0
                    and low <= prev_low + atr * near_level_atr
                    and lower_wick_atr >= min_wick_atr
                    and close >= bar_close * 0.996
                    and close_position >= 0.50
                    and 0.0 <= long_drift_atr <= max_entry_drift_atr
                    and max(bar_vol_ratio, vol_ratio) >= min_volume_ratio
                ):
                    direction = "long"
                    level = prev_low
                    sweep_extreme = low
                    signal_lag = recent.height - 1 - local_idx
                    vol_ratio = max(vol_ratio, bar_vol_ratio)
                    entry_drift_atr = long_drift_atr
                    reclaim_quality = float(params.get("weak_reclaim_penalty", 0.84))
                    break
                if (
                    prev_high > 0.0
                    and high >= prev_high - atr * near_level_atr
                    and upper_wick_atr >= min_wick_atr
                    and close <= bar_close * 1.004
                    and close_position <= 0.50
                    and 0.0 <= short_drift_atr <= max_entry_drift_atr
                    and max(bar_vol_ratio, vol_ratio) >= min_volume_ratio
                ):
                    direction = "short"
                    level = prev_high
                    sweep_extreme = high
                    signal_lag = recent.height - 1 - local_idx
                    vol_ratio = max(vol_ratio, bar_vol_ratio)
                    entry_drift_atr = short_drift_atr
                    reclaim_quality = float(params.get("weak_reclaim_penalty", 0.84))
                    break
        if direction is None:
            _reject(
                prepared,
                setup_id,
                "stop_hunt_not_detected",
                min_volume_ratio=min_volume_ratio,
                signal_lookback_bars=signal_lookback_bars,
            )
            return None

    max_signal_lag = max(0, min(int(params.get("max_signal_lag_bars", 3)), 8))
    if signal_lag > max_signal_lag:
        _reject(
            prepared,
            setup_id,
            "stop_hunt_stale_sweep",
            signal_lag=signal_lag,
            max_signal_lag=max_signal_lag,
        )
        return None

    flow_ok, _flow_details = orderflow_supports_reversal(
        prepared,
        direction,
        min_delta_long=float(params.get("min_recovery_delta_long", 0.49)),
        max_delta_short=float(params.get("max_recovery_delta_short", 0.51)),
        max_adverse_depth=float(params.get("max_adverse_depth_imbalance", 0.08)),
        max_adverse_micro=float(params.get("max_adverse_microprice_bias", 0.08)),
    )
    orderflow_penalty = float(params.get("orderflow_conflict_penalty", 0.86))
    reclaim_quality = reclaim_quality * (1.0 if flow_ok else orderflow_penalty)

    confirmation_atr_mult = 0.5
    expected_level = (
        level + (atr * confirmation_atr_mult)
        if direction == "long"
        else level - (atr * confirmation_atr_mult)
    )
    if direction == "long" and close < expected_level:
        _reject(
            prepared,
            setup_id,
            "stop_hunt_insufficient_close",
            close=close,
            expected_level=expected_level,
            atr=atr,
        )
        return None
    if direction == "short" and close > expected_level:
        _reject(
            prepared,
            setup_id,
            "stop_hunt_insufficient_close",
            close=close,
            expected_level=expected_level,
            atr=atr,
        )
        return None
    return _build_atr_signal(
        prepared=prepared,
        setup_id=setup_id,
        direction=direction,
        params=params,
        confirmed_bar=True,
        reasons=[
            f"stop_hunt_{direction}",
            f"swept_level={level:.4f}",
            f"signal_lag={signal_lag}",
            f"vol_ratio={vol_ratio:.2f}",
            f"entry_drift_atr={entry_drift_atr:.2f}",
            f"reclaim_quality={reclaim_quality:.2f}",
            f"orderflow_ok={flow_ok}",
        ],
        family=family,
        structure_clarity=0.7 * reclaim_quality,
        entry_anchor=level if level > 0.0 else None,
        stop_anchor=sweep_extreme if sweep_extreme > 0.0 else None,
    )


def detect_liquidity_sweep_setup(
    prepared: PreparedSymbol,
    settings: BotSettings,
    defaults: dict[str, float],
    effective: dict[str, float],
    setup_id: str,
    family: str,
) -> Signal | None:
    spec_kwargs = {
        "sweep_atr_mult": float(effective.get("sweep_atr_mult", defaults["sweep_atr_mult"])),
    }
    signal = run_setup_detection(
        prepared=prepared,
        settings=settings,
        setup_id=setup_id,
        family=family,
        defaults=defaults,
        effective=effective,
        spec_detect=detect_liquidity_sweep,
        extended_detect=_detect_liquidity_sweep_extended,
        spec_kwargs=spec_kwargs,
    )
    if signal is not None:
        return signal
    merged_params = {**defaults, **effective}
    fakeout_signal = _detect_fakeout_reclaim(
        prepared,
        settings,
        merged_params,
        setup_id=setup_id,
        family=family,
    )
    if fakeout_signal is not None:
        return fakeout_signal
    return _detect_stop_hunt_fallback(
        prepared,
        settings,
        merged_params,
        setup_id=setup_id,
        family=family,
    )


__all__ = [
    "_detect_liquidity_sweep_extended",
    "detect_liquidity_sweep",
    "detect_liquidity_sweep_setup",
]


class LiquiditySweepSetup(SpecDetectorSetup):
    setup_id = "liquidity_sweep"
    ENTRY_ORDER_TYPE: ClassVar[str] = "limit"
    family = "reversal"
    confirmation_profile = "countertrend_exhaustion"
    required_context = ("futures_flow",)

    DEFAULTS: ClassVar[dict[str, float]] = {
        "base_score": 0.62,
        "equal_level_tol": 0.0015,
        "threshold_tol": 0.0015,
        "min_level_hits": 2,
        "sweep_atr_mult": 0.20,
        "reclaim_threshold": 0.30,
        "reclaim_timeout_bars": 4.0,
        "max_sweep_age_bars": 8,
        "max_entry_distance_atr": 1.25,
        "sl_buffer_atr": 0.20,
        "bias_mismatch_penalty": 0.75,
        "min_rr": 1.9,
        "fakeout_lookback_bars": 20.0,
        "fakeout_window_bars": 3.0,
        "structure_clarity": 0.5,
        "sweep_tolerance_pct": 0.0010,
        "signal_lookback_bars": 20.0,
        "near_level_atr": 0.35,
        "min_wick_atr": 0.35,
        "max_entry_drift_atr": 1.25,
        "max_signal_lag_bars": 3.0,
        "weak_reclaim_penalty": 0.84,
        "min_recovery_delta_long": 0.49,
        "max_recovery_delta_short": 0.51,
        "max_adverse_depth_imbalance": 0.08,
        "max_adverse_microprice_bias": 0.08,
        "orderflow_conflict_penalty": 0.86,
    }

    detect_setup = detect_liquidity_sweep_setup

    def detect(self, prepared: PreparedSymbol, settings: BotSettings) -> Signal | None:
        return super().detect(prepared, settings)


__all__ = ["LiquiditySweepSetup"]
