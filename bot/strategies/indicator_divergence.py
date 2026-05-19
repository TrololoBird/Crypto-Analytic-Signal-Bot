"""Multi-indicator regular divergence/convergence setup.

Bearish divergence: price makes a higher high while oscillator highs weaken.
Bullish convergence: price makes a lower low while oscillator lows strengthen.
The detector uses prepared Polars feature columns (RSI, MACD histogram, OBV,
delta_ratio proxy) and does not recompute indicators inside strategy logic.
"""

from __future__ import annotations

import math

import polars as pl

from ..domain.config import BotSettings
from ..domain.schemas import PreparedSymbol, Signal
from ..features import _swing_points
from ..setup_base import BaseSetup
from ..setups import _build_signal, _compute_dynamic_score, _reject
from ..setups.utils import get_dynamic_params
from .common import as_float as _as_float


def _tail_pair(values: list[float]) -> tuple[float, float] | None:
    finite = [float(value) for value in values if math.isfinite(float(value))]
    if len(finite) < 2:
        return None
    return finite[-2], finite[-1]


def _swing_values(
    frame: pl.DataFrame,
    mask: pl.Series,
    *,
    price_column: str,
    indicator_column: str,
) -> tuple[tuple[float, float], tuple[float, float]] | None:
    if indicator_column not in frame.columns:
        return None
    rows = frame.filter(mask).select([price_column, indicator_column]).drop_nulls()
    if rows.height < 2:
        return None
    price_pair = _tail_pair([_as_float(value, math.nan) for value in rows[price_column].to_list()])
    indicator_pair = _tail_pair(
        [_as_float(value, math.nan) for value in rows[indicator_column].to_list()]
    )
    if price_pair is None or indicator_pair is None:
        return None
    return price_pair, indicator_pair


class IndicatorDivergenceSetup(BaseSetup):
    setup_id = "indicator_divergence"
    family = "reversal"
    confirmation_profile = "divergence_reversal"
    required_context = ("futures_flow",)
    required_features = ("rsi14", "macd_hist", "obv", "delta_ratio")

    def get_optimizable_params(self, settings: BotSettings | None = None) -> dict[str, float]:
        defaults = {
            "base_score": 0.53,
            "swing_lookback": 4.0,
            "min_price_delta_pct": 0.12,
            "min_indicator_votes": 2.0,
            "min_volume_ratio": 0.75,
            "max_rsi_long": 55.0,
            "min_rsi_short": 45.0,
            "sl_buffer_atr": 0.90,
            "min_rr": 1.9,
        }
        if settings is not None:
            setups = getattr(getattr(settings, "filters", None), "setups", {})
            if isinstance(setups, dict) and self.setup_id in setups:
                return {**defaults, **setups.get(self.setup_id, {})}
        return defaults

    def detect(self, prepared: PreparedSymbol, settings: BotSettings) -> Signal | None:
        setup_id = self.setup_id
        params = {
            **self.get_optimizable_params(settings),
            **get_dynamic_params(prepared, setup_id),
        }
        work = prepared.work_15m
        if work.height < 80:
            _reject(prepared, setup_id, "insufficient_15m_bars", bars=work.height)
            return None

        required = ("high", "low", "close", "atr14", "rsi14", "macd_hist", "volume_ratio20")
        missing = [column for column in required if column not in work.columns]
        if missing:
            _reject(prepared, setup_id, "missing_feature_columns", missing_fields=missing)
            return None

        close = _as_float(work.item(-1, "close"))
        atr = _as_float(work.item(-1, "atr14"))
        rsi = _as_float(work.item(-1, "rsi14"), 50.0)
        vol_ratio = _as_float(work.item(-1, "volume_ratio20"), 1.0)
        if min(close, atr) <= 0.0:
            _reject(prepared, setup_id, "invalid_indicator_state", close=close, atr=atr)
            return None
        if vol_ratio < float(params["min_volume_ratio"]):
            _reject(
                prepared,
                setup_id,
                "volume_confirmation_missing",
                volume_ratio=vol_ratio,
                min_volume_ratio=params["min_volume_ratio"],
            )
            return None

        sh_mask, sl_mask = _swing_points(
            work,
            n=max(2, int(params["swing_lookback"])),
            include_unconfirmed_tail=True,
        )
        indicator_columns = ("rsi14", "macd_hist", "obv", "delta_ratio")
        bullish_votes: list[str] = []
        bearish_votes: list[str] = []
        low_pair: tuple[float, float] | None = None
        high_pair: tuple[float, float] | None = None

        for column in indicator_columns:
            low_values = _swing_values(
                work,
                sl_mask,
                price_column="low",
                indicator_column=column,
            )
            if low_values is not None:
                price_pair, indicator_pair = low_values
                low_pair = price_pair
                old_price, new_price = price_pair
                old_indicator, new_indicator = indicator_pair
                price_delta_pct = (new_price / old_price - 1.0) * 100.0 if old_price else 0.0
                if (
                    price_delta_pct <= -float(params["min_price_delta_pct"])
                    and new_indicator > old_indicator
                ):
                    bullish_votes.append(column)

            high_values = _swing_values(
                work,
                sh_mask,
                price_column="high",
                indicator_column=column,
            )
            if high_values is not None:
                price_pair, indicator_pair = high_values
                high_pair = price_pair
                old_price, new_price = price_pair
                old_indicator, new_indicator = indicator_pair
                price_delta_pct = (new_price / old_price - 1.0) * 100.0 if old_price else 0.0
                if (
                    price_delta_pct >= float(params["min_price_delta_pct"])
                    and new_indicator < old_indicator
                ):
                    bearish_votes.append(column)

        min_votes = max(1, int(params["min_indicator_votes"]))
        direction: str | None = None
        reference_level: float | None = None
        votes: list[str] = []
        if len(bullish_votes) >= min_votes and rsi <= float(params["max_rsi_long"]):
            direction = "long"
            reference_level = low_pair[1] if low_pair is not None else _as_float(work["low"].tail(8).min())
            votes = bullish_votes
        if (
            len(bearish_votes) >= min_votes
            and len(bearish_votes) > len(votes)
            and rsi >= float(params["min_rsi_short"])
        ):
            direction = "short"
            reference_level = (
                high_pair[1] if high_pair is not None else _as_float(work["high"].tail(8).max())
            )
            votes = bearish_votes

        if direction is None or reference_level is None:
            _reject(
                prepared,
                setup_id,
                "regular_divergence_missing",
                bullish_votes=len(bullish_votes),
                bearish_votes=len(bearish_votes),
                min_indicator_votes=min_votes,
            )
            return None

        sl_buffer = float(params["sl_buffer_atr"])
        min_rr = float(params["min_rr"])
        if direction == "long":
            stop = min(reference_level, close) - atr * sl_buffer
            risk = close - stop
            tp1 = close + risk * min_rr
            tp2 = close + risk * max(min_rr + 0.4, 2.2)
        else:
            stop = max(reference_level, close) + atr * sl_buffer
            risk = stop - close
            tp1 = close - risk * min_rr
            tp2 = close - risk * max(min_rr + 0.4, 2.2)
        if risk <= 0.0:
            _reject(prepared, setup_id, "invalid_stop", direction=direction, stop=stop)
            return None

        score = _compute_dynamic_score(
            direction=direction,
            base_score=float(params["base_score"]),
            vol_ratio=vol_ratio,
            rsi=rsi,
            structure_clarity=min(1.0, len(votes) / float(len(indicator_columns))),
        )
        reasons = [
            f"indicator_{'convergence' if direction == 'long' else 'divergence'}_{direction}",
            f"votes={','.join(votes)}",
            f"rsi={rsi:.1f}",
            f"vol_ratio={vol_ratio:.2f}",
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
            price_anchor=close,
            atr=atr,
        )
