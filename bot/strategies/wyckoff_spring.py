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

class WyckoffSpringSetup(RoadmapSetup):
    setup_id = "wyckoff_spring"
    family = "reversal"
    confirmation_profile = "countertrend_exhaustion"
    required_context = ("futures_flow",)
    DEFAULTS = {
        **RoadmapSetup.DEFAULTS,
        "sweep_tolerance_pct": 0.0010,
        "min_volume_ratio": 1.05,
        "min_close_position_long": 0.55,
        "max_close_position_short": 0.45,
        "signal_lookback_bars": 12,
        "near_range_atr": 0.40,
        "min_wick_atr": 0.25,
        "volume_penalty": 0.90,
    }

    def detect(self, prepared: PreparedSymbol, settings: BotSettings) -> Signal | None:
        params = self._params(prepared, settings)
        work = prepared.work_15m
        missing = _missing_columns(
            work,
            ("high", "low", "close", "prev_donchian_low20", "prev_donchian_high20"),
        )
        if missing:
            _reject(prepared, self.setup_id, "missing_columns", missing_fields=missing)
            return None
        close = _last(work, "close")
        tolerance = float(params["sweep_tolerance_pct"])
        lookback = max(1, int(params.get("signal_lookback_bars", 12)))
        recent = work.tail(min(lookback, work.height))
        direction = None
        signal_lag = 0
        level = 0.0
        vol_ratio = _last(work, "volume_ratio20", 1.0)
        volume_penalty = vol_ratio < float(params["min_volume_ratio"])
        for local_idx in range(recent.height - 1, -1, -1):
            high = _as_float(recent.item(local_idx, "high"))
            low = _as_float(recent.item(local_idx, "low"))
            bar_close = _as_float(recent.item(local_idx, "close"))
            prev_low = _as_float(recent.item(local_idx, "prev_donchian_low20"))
            prev_high = _as_float(recent.item(local_idx, "prev_donchian_high20"))
            close_position = _as_float(recent.item(local_idx, "close_position"), 0.5)
            bar_vol_ratio = _as_float(recent.item(local_idx, "volume_ratio20"), 1.0)
            vol_ratio = max(vol_ratio, bar_vol_ratio)
            if (
                low < prev_low * (1.0 - tolerance)
                and max(bar_close, close) > prev_low
                and close_position >= float(params["min_close_position_long"])
            ):
                direction = "long"
                signal_lag = recent.height - 1 - local_idx
                level = prev_low
                break
            if (
                high > prev_high * (1.0 + tolerance)
                and min(bar_close, close) < prev_high
                and close_position <= float(params["max_close_position_short"])
            ):
                direction = "short"
                signal_lag = recent.height - 1 - local_idx
                level = prev_high
                break
        if direction is None:
            atr = _last(work, "atr14")
            near_range_atr = float(params.get("near_range_atr", 0.40))
            min_wick_atr = float(params.get("min_wick_atr", 0.25))
            if atr > 0.0 and recent.height > 0:
                range_low = _as_float(recent["low"].min())
                range_high = _as_float(recent["high"].max())
                for local_idx in range(recent.height - 1, -1, -1):
                    high = _as_float(recent.item(local_idx, "high"))
                    low = _as_float(recent.item(local_idx, "low"))
                    bar_close = _as_float(recent.item(local_idx, "close"))
                    open_ = (
                        _as_float(recent.item(local_idx, "open"))
                        if "open" in recent.columns
                        else bar_close
                    )
                    close_position = _as_float(recent.item(local_idx, "close_position"), 0.5)
                    lower_wick_atr = (min(open_, bar_close) - low) / atr
                    upper_wick_atr = (high - max(open_, bar_close)) / atr
                    if (
                        low <= range_low + atr * near_range_atr
                        and lower_wick_atr >= min_wick_atr
                        and max(bar_close, close) >= range_low + atr * 0.15
                        and close_position >= float(params["min_close_position_long"])
                    ):
                        direction = "long"
                        signal_lag = recent.height - 1 - local_idx
                        level = range_low
                        break
                    if (
                        high >= range_high - atr * near_range_atr
                        and upper_wick_atr >= min_wick_atr
                        and min(bar_close, close) <= range_high - atr * 0.15
                        and close_position <= float(params["max_close_position_short"])
                    ):
                        direction = "short"
                        signal_lag = recent.height - 1 - local_idx
                        level = range_high
                        break
            if direction is None:
                _reject(prepared, self.setup_id, "wyckoff_spring_upthrust_missing")
                return None
        clarity = 0.75 * (float(params.get("volume_penalty", 0.90)) if volume_penalty else 1.0)
        return _build_atr_signal(
            prepared=prepared,
            setup_id=self.setup_id,
            direction=direction,
            params=params,
            reasons=[
                f"wyckoff_{direction}",
                f"vol_ratio={vol_ratio:.2f}",
                f"signal_lag={signal_lag}",
                f"level={level:.4f}",
            ],
            family=self.family,
            structure_clarity=clarity,
        )


__all__ = ["WyckoffSpringSetup"]
