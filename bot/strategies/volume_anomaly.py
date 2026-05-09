"""Volume anomaly momentum setup."""

from __future__ import annotations

from ..config import BotSettings
from ..models import PreparedSymbol, Signal
from ..setup_base import BaseSetup
from ..setups import _build_signal, _compute_dynamic_score, _reject
from ..setups.utils import get_dynamic_params


def _as_float(value: object, default: float = 0.0) -> float:
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    return default


class VolumeAnomalySetup(BaseSetup):
    setup_id = "volume_anomaly"
    family = "breakout"
    confirmation_profile = "breakout_acceptance"
    required_context = ("futures_flow",)

    def get_optimizable_params(
        self, settings: BotSettings | None = None
    ) -> dict[str, float]:
        defaults = {
            "base_score": 0.52,
            "min_volume_ratio": 2.0,
            "min_body_atr": 0.75,
            "min_close_position_long": 0.72,
            "max_close_position_short": 0.28,
            "max_rsi_long": 78.0,
            "min_rsi_short": 22.0,
            "sl_buffer_atr": 0.6,
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

        required_columns = (
            "open",
            "high",
            "low",
            "close",
            "atr14",
            "volume_ratio20",
            "close_position",
            "rsi14",
        )
        missing = [column for column in required_columns if column not in work.columns]
        if missing:
            _reject(prepared, setup_id, "missing_columns", missing_fields=missing)
            return None

        params = {
            **self.get_optimizable_params(settings),
            **get_dynamic_params(prepared, setup_id),
        }
        open_ = _as_float(work.item(-1, "open"))
        high = _as_float(work.item(-1, "high"))
        low = _as_float(work.item(-1, "low"))
        close = _as_float(work.item(-1, "close"))
        atr = _as_float(work.item(-1, "atr14"))
        vol_ratio = _as_float(work.item(-1, "volume_ratio20"), 1.0)
        close_position = _as_float(work.item(-1, "close_position"), 0.5)
        rsi = _as_float(work.item(-1, "rsi14"), 50.0)
        if min(open_, high, low, close, atr) <= 0.0:
            _reject(prepared, setup_id, "invalid_indicator_state", atr=atr, close=close)
            return None

        min_volume_ratio = float(params["min_volume_ratio"])
        if vol_ratio < min_volume_ratio:
            _reject(
                prepared,
                setup_id,
                "volume_spike_missing",
                volume_ratio=vol_ratio,
                min_volume_ratio=min_volume_ratio,
            )
            return None

        body_atr = abs(close - open_) / atr if atr > 0.0 else 0.0
        min_body_atr = float(params["min_body_atr"])
        if body_atr < min_body_atr:
            _reject(
                prepared,
                setup_id,
                "body_too_small",
                body_atr=body_atr,
                min_body_atr=min_body_atr,
            )
            return None

        direction: str | None = None
        if (
            close > open_
            and close_position >= float(params["min_close_position_long"])
            and rsi <= float(params["max_rsi_long"])
        ):
            direction = "long"
        elif (
            close < open_
            and close_position <= float(params["max_close_position_short"])
            and rsi >= float(params["min_rsi_short"])
        ):
            direction = "short"
        if direction is None:
            _reject(
                prepared,
                setup_id,
                "candle_close_not_decisive",
                close_position=close_position,
                rsi=rsi,
            )
            return None

        sl_buffer = float(params["sl_buffer_atr"])
        min_rr = float(params["min_rr"])
        price_anchor = close
        if direction == "long":
            stop = min(low, open_) - atr * sl_buffer
            risk = price_anchor - stop
            tp1 = price_anchor + risk * min_rr
            tp2 = price_anchor + risk * max(min_rr, 2.0)
        else:
            stop = max(high, open_) + atr * sl_buffer
            risk = stop - price_anchor
            tp1 = price_anchor - risk * min_rr
            tp2 = price_anchor - risk * max(min_rr, 2.0)
        if risk <= 0.0:
            _reject(prepared, setup_id, "invalid_stop", stop=stop, close=close)
            return None

        score = _compute_dynamic_score(
            direction=direction,
            base_score=float(params["base_score"]),
            vol_ratio=vol_ratio,
            rsi=rsi,
            structure_clarity=min(body_atr / 2.0, 1.0),
        )
        reasons = [
            f"volume_anomaly_{direction}",
            f"vol_ratio={vol_ratio:.2f}",
            f"body_atr={body_atr:.2f}",
            f"close_position={close_position:.2f}",
        ]
        return _build_signal(
            prepared=prepared,
            setup_id=setup_id,
            direction=direction,
            score=score,
            timeframe="15m",
            reasons=reasons,
            strategy_family=self.family,
            stop=stop,
            tp1=tp1,
            tp2=tp2,
            price_anchor=price_anchor,
            atr=atr,
        )
