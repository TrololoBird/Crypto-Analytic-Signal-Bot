from __future__ import annotations

from ..domain.config import BotSettings
from ..domain.schemas import PreparedSymbol, Signal
from .roadmap_base import (
    RoadmapSetup,
    _as_float,
    _build_atr_signal,
    _last,
    _missing_columns,
    _reject,
)
from .spec_patterns import build_spec_signal, detect_stop_hunt

class StopHuntDetectionSetup(RoadmapSetup):
    setup_id = "stop_hunt_detection"
    family = "liquidity"
    confirmation_profile = "countertrend_exhaustion"
    required_context = ("futures_flow",)
    DEFAULTS = {
        **RoadmapSetup.DEFAULTS,
        "sweep_tolerance_pct": 0.0010,
        "min_volume_ratio": 0.80,
        "min_close_position_long": 0.45,
        "max_close_position_short": 0.55,
        "signal_lookback_bars": 12,
        "near_level_atr": 0.35,
        "min_wick_atr": 0.35,
    }

    def detect(self, prepared: PreparedSymbol, settings: BotSettings) -> Signal | None:
        params = self._params(prepared, settings)
        work = prepared.work_15m
        hit = detect_stop_hunt(work, timeframe="15m")
        if hit is not None:
            return build_spec_signal(
                prepared=prepared,
                settings=settings,
                setup_id=self.setup_id,
                family=self.family,
                hit=hit,
                defaults=self.DEFAULTS,
                params=params,
            )

        # FIX 2026-05-21: spec sweep is last-window strict; preserve the existing
        # recent sweep/wick-confirmation fallback before emitting a reject.
        missing = _missing_columns(
            work,
            ("high", "low", "close", "prev_donchian_low20", "prev_donchian_high20"),
        )
        if missing:
            _reject(prepared, self.setup_id, "missing_columns", missing_fields=missing)
            return None
        close = _last(work, "close")
        tolerance = float(params["sweep_tolerance_pct"])
        recent = work.tail(min(int(params.get("signal_lookback_bars", 3)), work.height))
        direction = None
        level = 0.0
        signal_lag = 0
        vol_ratio = _last(work, "volume_ratio20", 1.0)
        for local_idx in range(recent.height - 1, -1, -1):
            high = _as_float(recent.item(local_idx, "high"))
            low = _as_float(recent.item(local_idx, "low"))
            bar_close = _as_float(recent.item(local_idx, "close"))
            prev_low = _as_float(recent.item(local_idx, "prev_donchian_low20"))
            prev_high = _as_float(recent.item(local_idx, "prev_donchian_high20"))
            close_position = _as_float(recent.item(local_idx, "close_position"), 0.5)
            bar_vol_ratio = _as_float(recent.item(local_idx, "volume_ratio20"), 1.0)
            volume_ok = bar_vol_ratio >= float(params["min_volume_ratio"])
            if (
                low < prev_low * (1.0 - tolerance)
                and max(bar_close, close) > prev_low
                and close_position >= float(params["min_close_position_long"])
                and volume_ok
            ):
                direction = "long"
                level = prev_low
            elif (
                high > prev_high * (1.0 + tolerance)
                and min(bar_close, close) < prev_high
                and close_position <= float(params["max_close_position_short"])
                and volume_ok
            ):
                direction = "short"
                level = prev_high
            if direction is not None:
                signal_lag = recent.height - 1 - local_idx
                vol_ratio = max(vol_ratio, bar_vol_ratio)
                break
        if direction is None:
            atr = _last(work, "atr14")
            if atr > 0.0 and recent.height > 0:
                near_level_atr = float(params.get("near_level_atr", 0.35))
                min_wick_atr = float(params.get("min_wick_atr", 0.35))
                for local_idx in range(recent.height - 1, -1, -1):
                    open_ = _as_float(recent.item(local_idx, "open"))
                    high = _as_float(recent.item(local_idx, "high"))
                    low = _as_float(recent.item(local_idx, "low"))
                    bar_close = _as_float(recent.item(local_idx, "close"))
                    prev_low = _as_float(recent.item(local_idx, "prev_donchian_low20"))
                    prev_high = _as_float(recent.item(local_idx, "prev_donchian_high20"))
                    lower_wick_atr = (min(open_, bar_close) - low) / atr
                    upper_wick_atr = (high - max(open_, bar_close)) / atr
                    if (
                        low <= prev_low + atr * near_level_atr
                        and lower_wick_atr >= min_wick_atr
                        and close >= bar_close * 0.996
                    ):
                        direction = "long"
                        level = prev_low
                        signal_lag = recent.height - 1 - local_idx
                        break
                    if (
                        high >= prev_high - atr * near_level_atr
                        and upper_wick_atr >= min_wick_atr
                        and close <= bar_close * 1.004
                    ):
                        direction = "short"
                        level = prev_high
                        signal_lag = recent.height - 1 - local_idx
                        break
            if direction is None:
                _reject(prepared, self.setup_id, "stop_hunt_not_detected")
                return None
        return _build_atr_signal(
            prepared=prepared,
            setup_id=self.setup_id,
            direction=direction,
            params=params,
            reasons=[
                f"stop_hunt_{direction}",
                f"swept_level={level:.4f}",
                f"signal_lag={signal_lag}",
                f"vol_ratio={vol_ratio:.2f}",
            ],
            family=self.family,
            structure_clarity=0.7,
        )


__all__ = ["StopHuntDetectionSetup"]
