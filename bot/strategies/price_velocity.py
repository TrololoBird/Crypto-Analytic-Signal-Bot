"""Price velocity breakout setup.

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


class PriceVelocitySetup(BaseSetup):
    setup_id = "price_velocity"
    family = "breakout"
    confirmation_profile = "breakout_acceptance"
    required_context = ("futures_flow",)

    def get_optimizable_params(
        self, settings: BotSettings | None = None
    ) -> dict[str, float]:
        defaults = {
            "base_score": 0.53,
            "min_roc10_abs_pct": 0.75,
            "min_body_atr": 0.55,
            "min_volume_ratio": 1.35,
            "max_rsi_long": 82.0,
            "min_rsi_short": 18.0,
            "sl_buffer_atr": 0.55,
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
            "roc10",
            "volume_ratio20",
            "close_position",
            "rsi14",
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
        roc10 = as_float(work.item(-1, "roc10"))
        vol_ratio = as_float(work.item(-1, "volume_ratio20"), 1.0)
        close_position = as_float(work.item(-1, "close_position"), 0.5)
        rsi = as_float(work.item(-1, "rsi14"), 50.0)

        if min(open_, high, low, close, atr) <= 0.0:
            _reject(prepared, setup_id, "invalid_indicator_state", atr=atr)
            return None

        min_roc = params["min_roc10_abs_pct"]
        body_atr = abs(close - open_) / atr
        if abs(roc10) < min_roc and body_atr < params["min_body_atr"]:
            _reject(
                prepared,
                setup_id,
                "velocity_too_low",
                roc10=roc10,
                body_atr=body_atr,
            )
            return None
        if vol_ratio < params["min_volume_ratio"]:
            _reject(prepared, setup_id, "volume_too_low", volume_ratio=vol_ratio)
            return None

        direction: str | None = None
        if roc10 > 0.0 and close > open_ and close_position >= 0.65:
            direction = "long"
        elif roc10 < 0.0 and close < open_ and close_position <= 0.35:
            direction = "short"

        if direction is None:
            _reject(prepared, setup_id, "direction_not_confirmed", rsi=rsi)
            return None

        sl_buffer = params["sl_buffer_atr"]
        min_rr = params["min_rr"]
        if direction == "long":
            stop = min(low, open_) - atr * sl_buffer
            risk = close - stop
            tp1 = close + risk * min_rr
            tp2 = close + risk * max(2.0, min_rr + 0.35)
        else:
            stop = max(high, open_) + atr * sl_buffer
            risk = stop - close
            tp1 = close - risk * min_rr
            tp2 = close - risk * max(2.0, min_rr + 0.35)

        if risk <= 0.0:
            _reject(prepared, setup_id, "invalid_stop", stop=stop, close=close)
            return None

        return _build_standard_signal(
            prepared=prepared,
            setup_id=setup_id,
            direction=direction,
            params=params,
            vol_ratio=vol_ratio,
            rsi=rsi,
            structure_clarity=min(abs(roc10) / 2.5, 1.0),
            stop=stop,
            tp1=tp1,
            tp2=tp2,
            price_anchor=close,
            atr=atr,
            timeframe="15m",
            strategy_family=self.family,
            extra_reasons=[
                f"roc10={roc10:.2f}",
                f"body_atr={body_atr:.2f}",
                f"vol_ratio={vol_ratio:.2f}",
            ],
        )
