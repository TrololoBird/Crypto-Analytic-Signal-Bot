"""stop_hunt_detection — canonical strategy detector."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

import polars as pl

from ..domain.strategy_catalog import catalog_default_params
from ._common import SpecHit, _latest_values, build_spec_signal, with_spec_columns
from ._roadmap import _as_float, _build_atr_signal, _last, _missing_columns, _reject
from .common import orderflow_supports_reversal
from .roadmap_base import RoadmapSetup

if TYPE_CHECKING:
    from ..domain.config import BotSettings
    from ..domain.schemas import PreparedSymbol, Signal

__all__ = ["detect_stop_hunt", "detect_stop_hunt_prepared"]


def detect_stop_hunt(frame: pl.DataFrame, *, timeframe: str = "15m") -> SpecHit | None:
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
            strategy="stop_hunt_detection",
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
            strategy="stop_hunt_detection",
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


def detect_stop_hunt_prepared(
    prepared: PreparedSymbol,
    settings: BotSettings,
    effective_params: dict[str, float],
    *,
    setup_id: str,
    family: str,
) -> Signal | None:
    params = effective_params
    work = prepared.work_15m
    hit = detect_stop_hunt(work, timeframe="15m")
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

    # FIX 2026-05-21: spec sweep is last-window strict; preserve the existing
    # recent sweep/wick-confirmation fallback before emitting a reject.
    missing = _missing_columns(
        work,
        ("high", "low", "close", "prev_donchian_low20", "prev_donchian_high20"),
    )
    if missing:
        _reject(prepared, setup_id, "missing_columns", missing_fields=missing)
        return None
    close = _last(work, "close")
    atr = _last(work, "atr14")
    tolerance = max(0.0003, min(float(params["sweep_tolerance_pct"]), 0.0012))
    min_volume_ratio = max(float(params["min_volume_ratio"]), 0.80)
    min_close_position_long = max(float(params["min_close_position_long"]), 0.55)
    max_close_position_short = min(float(params["max_close_position_short"]), 0.45)
    signal_lookback_bars = max(5, min(int(params.get("signal_lookback_bars", 20)), 24))
    max_entry_drift_atr = max(0.50, min(float(params.get("max_entry_drift_atr", 1.25)), 2.0))
    recent = work.tail(min(signal_lookback_bars, work.height))
    direction = None
    level = 0.0
    signal_lag = 0
    sweep_extreme = 0.0
    vol_ratio = _last(work, "volume_ratio20", 1.0)
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

    # Additional confirmation: require close to be at least 0.5*ATR from swept level
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


detect_stop_hunt_detection = detect_stop_hunt_prepared


class StopHuntDetectionSetup(RoadmapSetup):
    setup_id = "stop_hunt_detection"
    family = "liquidity"
    confirmation_profile = "countertrend_exhaustion"
    required_context = ("futures_flow",)
    DEFAULTS: ClassVar[dict[str, float]] = {
        **RoadmapSetup.DEFAULTS,
        "sweep_tolerance_pct": 0.0010,
        "min_volume_ratio": 0.80,
        "min_close_position_long": 0.48,
        "max_close_position_short": 0.52,
        "signal_lookback_bars": 24,
        "near_level_atr": 0.35,
        "min_wick_atr": 0.35,
        "max_entry_drift_atr": 1.25,
        "max_signal_lag_bars": 6,
        "weak_reclaim_penalty": 0.84,
        "sl_buffer_atr": 1.20,
        "min_recovery_delta_long": 0.49,
        "max_recovery_delta_short": 0.51,
        "max_adverse_depth_imbalance": 0.08,
        "max_adverse_microprice_bias": 0.08,
        "orderflow_conflict_penalty": 0.86,
    }

    def detect(self, prepared: PreparedSymbol, settings: BotSettings) -> Signal | None:
        return detect_stop_hunt_prepared(
            prepared,
            settings,
            self._params(prepared, settings),
            setup_id=self.setup_id,
            family=self.family,
        )


__all__ = ["StopHuntDetectionSetup"]
