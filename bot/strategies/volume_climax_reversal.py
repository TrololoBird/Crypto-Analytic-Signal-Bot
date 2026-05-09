"""Volume climax reversal setup.

# WINDSURF_REVIEW: unified + vectorized + 1H context + graded
"""

from __future__ import annotations


from ..config import BotSettings
from ..models import PreparedSymbol, Signal
from ..setup_base import BaseSetup
from ..setups import _build_standard_signal, _reject, as_float
from ..setups.utils import (
    get_merged_params,
    validate_prepared_data,
)


class VolumeClimaxReversalSetup(BaseSetup):
    setup_id = "volume_climax_reversal"
    family = "reversal"
    confirmation_profile = "countertrend_exhaustion"
    required_context = ("futures_flow",)

    def get_optimizable_params(
        self, settings: BotSettings | None = None
    ) -> dict[str, float]:
        defaults = {
            "base_score": 0.52,
            "min_volume_ratio": 1.8,
            "min_wick_atr": 0.45,
            "max_rsi_long": 42.0,
            "min_rsi_short": 58.0,
            "sl_buffer_atr": 0.45,
            "min_rr": 1.5,
        }
        if settings is not None:
            setups = getattr(getattr(settings, "filters", None), "setups", {})
            if isinstance(setups, dict) and self.setup_id in setups:
                return {**defaults, **setups.get(self.setup_id, {})}
        return defaults

    def detect(self, prepared: PreparedSymbol, settings: BotSettings) -> Signal | None:
        setup_id = self.setup_id
        work = prepared.work_15m
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

        params = get_merged_params(
            setup_id, self.get_optimizable_params(settings), settings, prepared
        )
        if not validate_prepared_data(
            prepared, setup_id, required_columns=required, min_bars=30
        ):
            return None

        open_ = as_float(work.item(-1, "open"))
        high = as_float(work.item(-1, "high"))
        low = as_float(work.item(-1, "low"))
        close = as_float(work.item(-1, "close"))
        atr = as_float(work.item(-1, "atr14"))
        vol_ratio = as_float(work.item(-1, "volume_ratio20"), 1.0)
        close_position = as_float(work.item(-1, "close_position"), 0.5)
        rsi = as_float(work.item(-1, "rsi14"), 50.0)
        prev_low = as_float(work.item(-1, "prev_donchian_low20"))
        prev_high = as_float(work.item(-1, "prev_donchian_high20"))

        if min(open_, high, low, close, atr, prev_low, prev_high) <= 0.0:
            _reject(prepared, setup_id, "invalid_indicator_state", atr=atr)
            return None

        if vol_ratio < params["min_volume_ratio"]:
            _reject(prepared, setup_id, "volume_climax_missing", volume_ratio=vol_ratio)
            return None

        lower_wick_atr = (min(open_, close) - low) / atr
        upper_wick_atr = (high - max(open_, close)) / atr
        min_wick_atr = params["min_wick_atr"]
        direction: str | None = None
        if (
            low < prev_low
            and close > prev_low
            and lower_wick_atr >= min_wick_atr
            and close_position >= 0.55
        ):
            direction = "long"
        elif (
            high > prev_high
            and close < prev_high
            and upper_wick_atr >= min_wick_atr
            and close_position <= 0.45
        ):
            direction = "short"

        if direction is None:
            _reject(
                prepared,
                setup_id,
                "no_volume_climax_reclaim",
                lower_wick_atr=lower_wick_atr,
                upper_wick_atr=upper_wick_atr,
                rsi=rsi,
            )
            return None

        sl_buffer = params["sl_buffer_atr"]
        min_rr = params["min_rr"]
        if direction == "long":
            stop = low - atr * sl_buffer
            clarity = min(lower_wick_atr / 2.0, 1.0)
        else:
            stop = high + atr * sl_buffer
            clarity = min(upper_wick_atr / 2.0, 1.0)

        risk = abs(close - stop)
        if risk <= 0.0:
            _reject(prepared, setup_id, "invalid_stop", stop=stop, close=close)
            return None

        if direction == "long":
            tp1 = close + risk * min_rr
            tp2 = close + risk * max(2.0, min_rr + 0.35)
        else:
            tp1 = close - risk * min_rr
            tp2 = close - risk * max(2.0, min_rr + 0.35)

        return _build_standard_signal(
            prepared=prepared,
            setup_id=setup_id,
            direction=direction,
            params=params,
            vol_ratio=vol_ratio,
            rsi=rsi,
            structure_clarity=clarity,
            stop=stop,
            tp1=tp1,
            tp2=tp2,
            price_anchor=close,
            atr=atr,
            timeframe="15m",
            strategy_family=self.family,
            extra_reasons=[
                f"vol_ratio={vol_ratio:.2f}",
                f"lower_wick_atr={lower_wick_atr:.2f}",
                f"upper_wick_atr={upper_wick_atr:.2f}",
            ],
        )
