"""Spec + prepared detector."""
from __future__ import annotations

import math

import polars as pl

from ._common import SpecHit, as_float, with_spec_columns, _latest_values, build_spec_signal
from ...domain.config import BotSettings
from ...domain.schemas import PreparedSymbol, Signal
from ...domain.strategy_catalog import catalog_default_params
from ._roadmap import (
    _build_atr_signal,
    _finite_or_none,
    _last,
    _prev,
    _reject,
)

__all__ = ['detect_aggression_shift', 'detect_aggression_shift_prepared']


def detect_aggression_shift(frame: pl.DataFrame, *, timeframe: str = "15m") -> SpecHit | None:
    work = with_spec_columns(frame)
    if work.height < 25:
        return None
    row = _latest_values(work)
    delta = row.get("spec_delta", 0.0)
    delta_mean = row.get("spec_abs_delta_mean20", 0.0)
    atr = row.get("spec_atr14", 0.0)
    if atr <= 0.0 or delta_mean <= 0.0 or abs(delta) < delta_mean * 2.0:
        return None
    price_up = row["close"] > as_float(work.item(-2, "close"))
    if price_up and delta < 0.0:
        return SpecHit(
            strategy="aggression_shift",
            direction="short",
            entry=row["close"],
            stop_basis=row["high"],
            atr=atr,
            timeframe=timeframe,
            reasons=(f"bearish_delta_vs_price delta_x={abs(delta)/delta_mean:.2f}",),
            structure_clarity=min(1.0, abs(delta) / max(delta_mean * 3.0, 1e-8)),
            vol_ratio=row.get("volume_ratio20", 1.0),
            rsi=row.get("rsi14", 50.0),
        )
    if not price_up and delta > 0.0:
        return SpecHit(
            strategy="aggression_shift",
            direction="long",
            entry=row["close"],
            stop_basis=row["low"],
            atr=atr,
            timeframe=timeframe,
            reasons=(f"bullish_delta_vs_price delta_x={abs(delta)/delta_mean:.2f}",),
            structure_clarity=min(1.0, abs(delta) / max(delta_mean * 3.0, 1e-8)),
            vol_ratio=row.get("volume_ratio20", 1.0),
            rsi=row.get("rsi14", 50.0),
        )
    return None


def _delta_shift_candidate(
    prepared: PreparedSymbol,
    params: dict[str, float],
) -> tuple[float, float, str, int, str] | None:
    work = prepared.work_15m
    configured = float(params.get("min_shift", 0.05))
    proxy_floor = float(params.get("min_proxy_shift", 0.025))
    spike_mult = float(params.get("delta_spike_mult", 2.0))
    lookback = max(1, min(int(params.get("signal_lookback_bars", 6)), 12))
    max_drift_atr = max(0.0, float(params.get("max_entry_drift_atr", 0.75)))
    if work.height >= 22 and "delta_ratio" in work.columns:
        latest_close = _last(work, "close")
        start_idx = max(21, work.height - lookback)
        for idx in range(work.height - 1, start_idx - 1, -1):
            raw_current = work.item(idx, "delta_ratio")
            try:
                current = float(raw_current) - 0.5
            except (TypeError, ValueError):
                continue
            if not math.isfinite(current):
                continue
            baseline_values: list[float] = []
            for raw_value in work["delta_ratio"].slice(max(0, idx - 20), min(20, idx)).to_list():
                try:
                    value = float(raw_value) - 0.5
                except (TypeError, ValueError):
                    continue
                if math.isfinite(value):
                    baseline_values.append(value)
            if len(baseline_values) < 6:
                continue
            mean_abs = sum(abs(value) for value in baseline_values) / len(baseline_values)
            threshold = max(configured, proxy_floor, mean_abs * spike_mult)
            if abs(current) < threshold:
                continue
            close = float(work.item(idx, "close") or 0.0)
            prev_close = float(work.item(idx - 1, "close") or 0.0)
            atr = float(work.item(idx, "atr14") or 0.0) if "atr14" in work.columns else 0.0
            if min(close, prev_close, latest_close) <= 0.0:
                continue
            price_up = close > prev_close
            price_down = close < prev_close
            if price_up and current < 0.0:
                direction = "short"
                if atr > 0.0 and latest_close > close + atr * max_drift_atr:
                    continue
            elif price_down and current > 0.0:
                direction = "long"
                if atr > 0.0 and latest_close < close - atr * max_drift_atr:
                    continue
            else:
                continue
            return (
                current,
                threshold,
                "ohlcv_delta_proxy",
                work.height - 1 - idx,
                direction,
            )

    explicit_shift = _finite_or_none(prepared.aggression_shift)
    if explicit_shift is None:
        return None
    threshold = max(configured, proxy_floor)
    if abs(explicit_shift) < threshold:
        return None
    close = _last(work, "close")
    prev_close = _prev(work, "close")
    if min(close, prev_close) <= 0.0:
        return None
    if close > prev_close and explicit_shift < 0.0:
        direction = "short"
    elif close < prev_close and explicit_shift > 0.0:
        direction = "long"
    else:
        return None
    return (
        explicit_shift,
        threshold,
        str(getattr(prepared, "orderflow_source", None) or "agg_trade"),
        0,
        direction,
    )


def detect_aggression_shift_prepared(
    prepared: PreparedSymbol,
    settings: BotSettings,
    effective_params: dict[str, float],
    *,
    setup_id: str,
    family: str,
) -> Signal | None:
    params = effective_params
    hit = detect_aggression_shift(prepared.work_15m, timeframe="15m")
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

    # FIX 2026-05-21: strict spec miss must fall through to the configured
    # public orderflow proxy path instead of making the fallback unreachable.
    candidate = _delta_shift_candidate(prepared, params)
    if candidate is None:
        _reject(
            prepared,
            setup_id,
            "pattern.aggression_shift_too_small",
            signal_lookback_bars=int(params.get("signal_lookback_bars", 6)),
        )
        return None
    shift, threshold, shift_source, signal_lag, direction = candidate
    vol_ratio = _last(prepared.work_15m, "volume_ratio20", 1.0)
    volume_penalty = vol_ratio < float(params["min_volume_ratio"])
    return _build_atr_signal(
        prepared=prepared,
        setup_id=setup_id,
        direction=direction,
        params=params,
        reasons=[
            f"aggression_shift_{direction}",
            f"shift={shift:.3f}",
            f"threshold={threshold:.3f}",
            f"flow_source={shift_source}",
            f"signal_lag={signal_lag}",
        ],
        family=family,
        structure_clarity=min(abs(shift) * 3.0, 1.0)
        * (0.72 if shift_source != "agg_trade" else 1.0)
        * (0.90 if volume_penalty else 1.0),
    )
