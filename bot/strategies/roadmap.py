"""Roadmap strategy detectors for public-data signal generation.

These setups are signal-only detectors. They intentionally use fields already
available in ``PreparedSymbol`` or prepared Polars frames; they do not call
exchange APIs and they do not place orders.
"""

from __future__ import annotations

from typing import ClassVar

import polars as pl

from ..domain.config import BotSettings
from ..domain.schemas import PreparedSymbol, Signal
from ..setup_base import BaseSetup
from ..setups import _build_signal, _compute_dynamic_score, _reject
from ..setups.utils import get_dynamic_params
from .common import (
    as_float as _as_float,
    finite_or_none as _finite_or_none,
    first_finite as _first_finite,
    last as _last,
    previous as _prev,
)


def _missing_columns(frame: pl.DataFrame, columns: tuple[str, ...]) -> list[str]:
    return [column for column in columns if column not in frame.columns]


def _configured_params(
    settings: BotSettings | None,
    setup_id: str,
    defaults: dict[str, float],
) -> dict[str, float]:
    if settings is None:
        return dict(defaults)
    setups = getattr(getattr(settings, "filters", None), "setups", {})
    if isinstance(setups, dict) and setup_id in setups:
        return {**defaults, **setups.get(setup_id, {})}
    return dict(defaults)


def _price_change_pct(frame: pl.DataFrame, bars: int = 8) -> float:
    if frame.height < 2 or "close" not in frame.columns:
        return 0.0
    anchor_idx = max(0, frame.height - max(2, bars) - 1)
    start = _as_float(frame.item(anchor_idx, "close"))
    end = _last(frame, "close")
    if start <= 0.0 or end <= 0.0:
        return 0.0
    return (end / start - 1.0) * 100.0


def _flow_delta(prepared: PreparedSymbol) -> float | None:
    direct_delta = _first_finite(
        prepared.agg_trade_delta_30s,
        prepared.aggression_shift,
    )
    if direct_delta is not None:
        return direct_delta
    taker_ratio = _finite_or_none(prepared.taker_ratio)
    if taker_ratio is not None:
        return taker_ratio - 1.0
    work = prepared.work_15m
    if work.is_empty() or "delta_ratio" not in work.columns:
        return None
    return _last(work, "delta_ratio", 0.5) - 0.5


def _confirmed_context_conflict(prepared: PreparedSymbol, direction: str) -> bool:
    context = (
        str(getattr(prepared, "bias_1h", "") or ""),
        str(getattr(prepared, "structure_1h", "") or ""),
        str(getattr(prepared, "regime_1h_confirmed", "") or ""),
    )
    if direction == "long":
        return sum(value == "downtrend" for value in context) >= 2
    if direction == "short":
        return sum(value == "uptrend" for value in context) >= 2
    return False


def _series_mean_tail(frame: pl.DataFrame, column: str, window: int) -> float:
    if frame.is_empty() or column not in frame.columns:
        return 0.0
    values = [
        _as_float(value)
        for value in frame[column].tail(max(1, int(window))).to_list()
        if value is not None
    ]
    return sum(values) / len(values) if values else 0.0


def _series_max_tail(frame: pl.DataFrame, column: str, window: int) -> float:
    if frame.is_empty() or column not in frame.columns:
        return 0.0
    values = [
        _as_float(value)
        for value in frame[column].tail(max(1, int(window))).to_list()
        if value is not None
    ]
    return max(values) if values else 0.0


def _series_min_tail(frame: pl.DataFrame, column: str, window: int) -> float:
    if frame.is_empty() or column not in frame.columns:
        return 0.0
    values = [
        _as_float(value)
        for value in frame[column].tail(max(1, int(window))).to_list()
        if value is not None
    ]
    return min(values) if values else 0.0


def _build_atr_signal(
    *,
    prepared: PreparedSymbol,
    setup_id: str,
    direction: str,
    params: dict[str, float],
    reasons: list[str],
    family: str,
    timeframe: str = "15m",
    structure_clarity: float = 0.5,
) -> Signal | None:
    work = prepared.work_15m
    close = _last(work, "close")
    high = _last(work, "high")
    low = _last(work, "low")
    atr = _last(work, "atr14")
    vol_ratio = _last(work, "volume_ratio20", 1.0)
    rsi = _last(work, "rsi14", 50.0)
    if min(close, high, low, atr) <= 0.0:
        _reject(prepared, setup_id, "invalid_indicator_state", close=close, atr=atr)
        return None

    sl_buffer = float(params.get("sl_buffer_atr", 0.65))
    min_rr = float(params.get("min_rr", 1.5))
    if direction == "long":
        stop = min(low, close - atr * sl_buffer) - atr * 0.05
        risk = close - stop
        tp1 = close + risk * min_rr
        tp2 = close + risk * max(min_rr + 0.4, 2.0)
    else:
        stop = max(high, close + atr * sl_buffer) + atr * 0.05
        risk = stop - close
        tp1 = close - risk * min_rr
        tp2 = close - risk * max(min_rr + 0.4, 2.0)
    if risk <= 0.0:
        _reject(prepared, setup_id, "invalid_stop", stop=stop, close=close)
        return None

    score = _compute_dynamic_score(
        direction=direction,
        base_score=float(params.get("base_score", 0.52)),
        vol_ratio=vol_ratio,
        rsi=rsi,
        structure_clarity=max(0.0, min(1.0, structure_clarity)),
    )
    return _build_signal(
        prepared=prepared,
        setup_id=setup_id,
        direction=direction,
        score=score,
        timeframe=timeframe,
        reasons=reasons,
        strategy_family=family,
        stop=stop,
        tp1=tp1,
        tp2=tp2,
        price_anchor=close,
        atr=atr,
    )


class RoadmapSetup(BaseSetup):
    DEFAULTS: ClassVar[dict[str, float]] = {
        "base_score": 0.52,
        "sl_buffer_atr": 0.65,
        "min_rr": 1.9,
    }

    def get_optimizable_params(self, settings: BotSettings | None = None) -> dict[str, float]:
        return _configured_params(settings, self.setup_id, self.DEFAULTS)

    def _params(self, prepared: PreparedSymbol, settings: BotSettings) -> dict[str, float]:
        return {
            **self.get_optimizable_params(settings),
            **get_dynamic_params(prepared, self.setup_id),
        }


class WhaleWallsSetup(RoadmapSetup):
    setup_id = "whale_walls"
    family = "orderbook"
    confirmation_profile = "breakout_acceptance"
    required_context = ("futures_flow",)
    DEFAULTS = {
        **RoadmapSetup.DEFAULTS,
        "min_depth_imbalance": 0.45,
        "min_microprice_bias": 0.20,
        "min_volume_ratio": 0.90,
        "min_close_position_long": 0.55,
        "max_close_position_short": 0.45,
        "max_spread_bps": 8.0,
        "min_roc10_abs_pct": 0.05,
        "min_rr": 1.9,
    }

    def detect(self, prepared: PreparedSymbol, settings: BotSettings) -> Signal | None:
        params = self._params(prepared, settings)
        depth = _finite_or_none(prepared.depth_imbalance)
        micro = _finite_or_none(prepared.microprice_bias)
        if depth is None or micro is None:
            _reject(
                prepared,
                self.setup_id,
                "orderbook_context_incomplete",
                depth_imbalance=depth,
                microprice_bias=micro,
            )
            return None
        spread = _finite_or_none(prepared.spread_bps)
        if spread is not None and spread > float(params["max_spread_bps"]):
            _reject(prepared, self.setup_id, "spread_too_wide", spread_bps=spread)
            return None
        depth_value = float(depth)
        micro_value = float(micro)
        work = prepared.work_15m
        vol_ratio = _last(work, "volume_ratio20", 1.0)
        volume_penalty = vol_ratio < float(params["min_volume_ratio"])
        close_position = _last(work, "close_position", 0.5)
        roc10 = _last(work, "roc10", _price_change_pct(work, 10))
        if abs(roc10) < float(params["min_roc10_abs_pct"]):
            _reject(prepared, self.setup_id, "price_acceptance_missing", roc10=roc10)
            return None
        long_votes = sum(
            (
                depth_value >= float(params["min_depth_imbalance"]),
                micro_value >= float(params["min_microprice_bias"]),
                close_position >= float(params["min_close_position_long"]),
                roc10 >= 0.0,
            )
        )
        short_votes = sum(
            (
                depth_value <= -float(params["min_depth_imbalance"]),
                micro_value <= -float(params["min_microprice_bias"]),
                close_position <= float(params["max_close_position_short"]),
                roc10 <= 0.0,
            )
        )
        if long_votes >= 3 and long_votes > short_votes:
            direction = "long"
        elif short_votes >= 3 and short_votes > long_votes:
            direction = "short"
        else:
            reason = (
                "wall_proxy_conflict" if depth_value * micro_value < 0.0 else "wall_proxy_too_weak"
            )
            _reject(
                prepared,
                self.setup_id,
                reason,
                depth_imbalance=depth_value,
                microprice_bias=micro_value,
                close_position=close_position,
            )
            return None
        context_penalty = _confirmed_context_conflict(prepared, direction)
        clarity = min(abs(depth_value), 1.0)
        if volume_penalty:
            clarity *= 0.90
        if context_penalty:
            clarity *= 0.82
        return _build_atr_signal(
            prepared=prepared,
            setup_id=self.setup_id,
            direction=direction,
            params=params,
            reasons=[
                f"orderbook_wall_proxy_{direction}",
                f"depth_imbalance={depth_value:.3f}",
                f"micro={micro_value:.3f}",
                f"close_position={close_position:.2f}",
                f"volume_ratio={vol_ratio:.2f}",
                f"roc10={roc10:.2f}",
                f"votes={long_votes if direction == 'long' else short_votes}",
            ],
            family=self.family,
            structure_clarity=clarity,
        )


class SpreadStrategySetup(RoadmapSetup):
    setup_id = "spread_strategy"
    family = "orderbook"
    confirmation_profile = "breakout_acceptance"
    required_context = ("futures_flow",)
    DEFAULTS = {
        **RoadmapSetup.DEFAULTS,
        "max_spread_bps": 8.0,
        "min_volume_ratio": 0.90,
        "min_roc10_abs_pct": 0.15,
        "min_depth_imbalance": 0.10,
        "min_microprice_bias": 0.05,
        "min_close_position_long": 0.55,
        "max_close_position_short": 0.45,
        "min_rr": 1.9,
    }

    def detect(self, prepared: PreparedSymbol, settings: BotSettings) -> Signal | None:
        params = self._params(prepared, settings)
        spread = _finite_or_none(prepared.spread_bps)
        if spread is None:
            _reject(prepared, self.setup_id, "spread_missing")
            return None
        if spread > float(params["max_spread_bps"]):
            _reject(prepared, self.setup_id, "spread_too_wide", spread_bps=spread)
            return None
        work = prepared.work_15m
        vol_ratio = _last(work, "volume_ratio20", 1.0)
        roc10 = _last(work, "roc10", _price_change_pct(work, 10))
        volume_penalty = vol_ratio < float(params["min_volume_ratio"])
        if abs(roc10) < float(params["min_roc10_abs_pct"]):
            _reject(prepared, self.setup_id, "momentum_too_low", roc10=roc10)
            return None
        direction = "long" if roc10 > 0.0 else "short"
        depth = _finite_or_none(prepared.depth_imbalance)
        micro = _finite_or_none(prepared.microprice_bias)
        if depth is None or micro is None:
            _reject(
                prepared,
                self.setup_id,
                "orderbook_context_incomplete",
                depth_imbalance=depth,
                microprice_bias=micro,
            )
            return None
        depth_value = depth
        micro_value = micro
        close_position = _last(work, "close_position", 0.5)
        if direction == "long":
            orderbook_ok = depth_value >= float(
                params["min_depth_imbalance"]
            ) and micro_value >= float(params["min_microprice_bias"])
            close_ok = close_position >= float(params["min_close_position_long"])
        else:
            orderbook_ok = depth_value <= -float(
                params["min_depth_imbalance"]
            ) and micro_value <= -float(params["min_microprice_bias"])
            close_ok = close_position <= float(params["max_close_position_short"])
        if not orderbook_ok and not close_ok:
            _reject(
                prepared,
                self.setup_id,
                "orderbook_not_aligned",
                depth_imbalance=depth_value,
                microprice_bias=micro_value,
                close_position=close_position,
            )
            return None
        context_penalty = _confirmed_context_conflict(prepared, direction)
        clarity = min(abs(roc10) / 1.5, 1.0)
        if not orderbook_ok or not close_ok:
            clarity *= 0.82
        if volume_penalty:
            clarity *= 0.90
        if context_penalty:
            clarity *= 0.82
        return _build_atr_signal(
            prepared=prepared,
            setup_id=self.setup_id,
            direction=direction,
            params=params,
            reasons=[
                f"tight_spread_{direction}",
                f"spread_bps={spread:.2f}",
                f"roc10={roc10:.2f}",
                f"depth={depth_value:.3f}",
                f"micro={micro_value:.5f}",
                f"close_position={close_position:.2f}",
            ],
            family=self.family,
            structure_clarity=clarity,
        )


class DepthImbalanceSetup(RoadmapSetup):
    setup_id = "depth_imbalance"
    family = "orderbook"
    confirmation_profile = "breakout_acceptance"
    required_context = ("futures_flow",)
    DEFAULTS = {
        **RoadmapSetup.DEFAULTS,
        "min_depth_imbalance": 0.20,
        "min_microprice_bias": 0.05,
        "min_close_position_long": 0.52,
        "max_close_position_short": 0.48,
        "min_volume_ratio": 0.80,
        "min_roc10_abs_pct": 0.00,
        "min_rr": 1.9,
    }

    def detect(self, prepared: PreparedSymbol, settings: BotSettings) -> Signal | None:
        params = self._params(prepared, settings)
        depth = _finite_or_none(prepared.depth_imbalance)
        if depth is None:
            _reject(prepared, self.setup_id, "depth_imbalance_missing")
            return None
        micro = _finite_or_none(prepared.microprice_bias)
        if micro is None:
            _reject(prepared, self.setup_id, "microprice_bias_missing")
            return None
        close_position = _last(prepared.work_15m, "close_position", 0.5)
        threshold = float(params["min_depth_imbalance"])
        micro_threshold = float(params["min_microprice_bias"])
        vol_ratio = _last(prepared.work_15m, "volume_ratio20", 1.0)
        volume_penalty = vol_ratio < float(params["min_volume_ratio"])
        roc10 = _last(prepared.work_15m, "roc10", _price_change_pct(prepared.work_15m, 10))
        if abs(roc10) < float(params["min_roc10_abs_pct"]):
            _reject(prepared, self.setup_id, "price_acceptance_missing", roc10=roc10)
            return None
        long_votes = sum(
            (
                depth >= threshold,
                micro >= micro_threshold,
                close_position >= float(params["min_close_position_long"]),
                roc10 >= 0.0,
            )
        )
        short_votes = sum(
            (
                depth <= -threshold,
                micro <= -micro_threshold,
                close_position <= float(params["max_close_position_short"]),
                roc10 <= 0.0,
            )
        )
        if long_votes >= 2 and long_votes > short_votes:
            direction = "long"
        elif short_votes >= 2 and short_votes > long_votes:
            direction = "short"
        else:
            _reject(
                prepared,
                self.setup_id,
                "depth_not_actionable",
                depth_imbalance=depth,
                microprice_bias=micro,
                close_position=close_position,
            )
            return None
        context_penalty = _confirmed_context_conflict(prepared, direction)
        clarity = min(abs(depth), 1.0)
        if volume_penalty:
            clarity *= 0.90
        if context_penalty:
            clarity *= 0.82
        return _build_atr_signal(
            prepared=prepared,
            setup_id=self.setup_id,
            direction=direction,
            params=params,
            reasons=[
                f"depth_imbalance_{direction}",
                f"depth={depth:.3f}",
                f"micro={micro:.3f}",
                f"volume_ratio={vol_ratio:.2f}",
                f"roc10={roc10:.2f}",
                f"votes={long_votes if direction == 'long' else short_votes}",
            ],
            family=self.family,
            structure_clarity=clarity,
        )


class AbsorptionSetup(RoadmapSetup):
    setup_id = "absorption"
    family = "orderflow"
    confirmation_profile = "countertrend_exhaustion"
    required_context = ("futures_flow",)
    DEFAULTS = {
        **RoadmapSetup.DEFAULTS,
        "min_abs_flow_delta": 0.05,
        "min_close_position_long": 0.55,
        "max_close_position_short": 0.45,
        "min_wick_atr": 0.12,
        "min_volume_ratio": 0.90,
    }

    def detect(self, prepared: PreparedSymbol, settings: BotSettings) -> Signal | None:
        params = self._params(prepared, settings)
        flow = _flow_delta(prepared)
        if flow is None:
            _reject(prepared, self.setup_id, "orderflow_delta_missing")
            return None
        work = prepared.work_15m
        close_position = _last(work, "close_position", 0.5)
        atr = _last(work, "atr14")
        high = _last(work, "high")
        low = _last(work, "low")
        close = _last(work, "close")
        open_ = _last(work, "open")
        if min(atr, high, low, close, open_) <= 0.0:
            _reject(prepared, self.setup_id, "invalid_indicator_state", atr=atr)
            return None
        lower_wick_atr = (min(open_, close) - low) / atr
        upper_wick_atr = (high - max(open_, close)) / atr
        vol_ratio = _last(work, "volume_ratio20", 1.0)
        volume_penalty = vol_ratio < float(params["min_volume_ratio"])
        if (
            flow <= -float(params["min_abs_flow_delta"])
            and close_position >= float(params["min_close_position_long"])
            and lower_wick_atr >= float(params["min_wick_atr"])
        ):
            direction = "long"
        elif (
            flow >= float(params["min_abs_flow_delta"])
            and close_position <= float(params["max_close_position_short"])
            and upper_wick_atr >= float(params["min_wick_atr"])
        ):
            direction = "short"
        else:
            _reject(prepared, self.setup_id, "absorption_not_confirmed", flow_delta=flow)
            return None
        clarity = min(abs(flow) * 2.0, 1.0)
        if volume_penalty:
            clarity *= 0.90
        return _build_atr_signal(
            prepared=prepared,
            setup_id=self.setup_id,
            direction=direction,
            params=params,
            reasons=[
                f"absorption_{direction}",
                f"flow_delta={flow:.3f}",
                f"close_position={close_position:.2f}",
                f"volume_ratio={vol_ratio:.2f}",
            ],
            family=self.family,
            structure_clarity=clarity,
        )


class AggressionShiftSetup(RoadmapSetup):
    setup_id = "aggression_shift"
    family = "orderflow"
    confirmation_profile = "breakout_acceptance"
    required_context = ("futures_flow",)
    DEFAULTS = {
        **RoadmapSetup.DEFAULTS,
        "min_shift": 0.05,
        "min_volume_ratio": 0.90,
    }

    def detect(self, prepared: PreparedSymbol, settings: BotSettings) -> Signal | None:
        params = self._params(prepared, settings)
        explicit_shift = _finite_or_none(prepared.aggression_shift)
        if explicit_shift is not None:
            shift = explicit_shift
        elif prepared.work_15m.height >= 6 and "delta_ratio" in prepared.work_15m.columns:
            current_delta = _last(prepared.work_15m, "delta_ratio", 0.5)
            shift = current_delta - _series_mean_tail(
                prepared.work_15m.head(prepared.work_15m.height - 1),
                "delta_ratio",
                5,
            )
        else:
            _reject(prepared, self.setup_id, "aggression_shift_missing")
            return None
        vol_ratio = _last(prepared.work_15m, "volume_ratio20", 1.0)
        volume_penalty = vol_ratio < float(params["min_volume_ratio"])
        if abs(shift) < float(params["min_shift"]) and "delta_ratio" in prepared.work_15m.columns:
            current_delta = _last(prepared.work_15m, "delta_ratio", 0.5)
            shift = current_delta - 0.5
        if shift >= float(params["min_shift"]):
            direction = "long"
        elif shift <= -float(params["min_shift"]):
            direction = "short"
        else:
            _reject(prepared, self.setup_id, "aggression_shift_too_small", shift=shift)
            return None
        return _build_atr_signal(
            prepared=prepared,
            setup_id=self.setup_id,
            direction=direction,
            params=params,
            reasons=[f"aggression_shift_{direction}", f"shift={shift:.3f}"],
            family=self.family,
            structure_clarity=min(abs(shift) * 3.0, 1.0)
            * (0.90 if volume_penalty else 1.0),
        )


class LiquidationHeatmapSetup(RoadmapSetup):
    setup_id = "liquidation_heatmap"
    family = "liquidity"
    confirmation_profile = "countertrend_exhaustion"
    required_context = ("futures_flow",)
    DEFAULTS = {
        **RoadmapSetup.DEFAULTS,
        "min_liquidation_score": 0.30,
        "min_proxy_volume_ratio": 1.20,
        "min_proxy_wick_atr": 0.25,
        "proxy_lookback_bars": 12,
        "min_close_position_long": 0.55,
        "max_close_position_short": 0.45,
        "min_volume_ratio": 0.90,
    }

    def detect(self, prepared: PreparedSymbol, settings: BotSettings) -> Signal | None:
        params = self._params(prepared, settings)
        score = _finite_or_none(prepared.liquidation_score)
        close_position = _last(prepared.work_15m, "close_position", 0.5)
        vol_ratio = _last(prepared.work_15m, "volume_ratio20", 1.0)
        volume_penalty = vol_ratio < float(params["min_volume_ratio"])
        if score is None:
            work = prepared.work_15m
            atr = _last(work, "atr14")
            close = _last(work, "close")
            if min(atr, close) <= 0.0:
                _reject(prepared, self.setup_id, "liquidation_score_missing")
                return None
            proxy_threshold = float(params["min_proxy_volume_ratio"])
            wick_threshold = float(params["min_proxy_wick_atr"])
            proxy_lookback = max(1, int(params.get("proxy_lookback_bars", 12)))
            recent = work.tail(min(proxy_lookback, work.height))
            best_proxy_score = 0.0
            best_lower_wick = 0.0
            best_upper_wick = 0.0
            for local_idx in range(recent.height - 1, -1, -1):
                open_ = _as_float(recent.item(local_idx, "open"))
                high = _as_float(recent.item(local_idx, "high"))
                low = _as_float(recent.item(local_idx, "low"))
                bar_close = _as_float(recent.item(local_idx, "close"))
                bar_volume = _as_float(recent.item(local_idx, "volume_ratio20"), 1.0)
                close_position_bar = _as_float(recent.item(local_idx, "close_position"), 0.5)
                if min(open_, high, low, bar_close) <= 0.0 or bar_volume < proxy_threshold:
                    continue
                lower_wick_atr = (min(open_, bar_close) - low) / atr
                upper_wick_atr = (high - max(open_, bar_close)) / atr
                long_proxy = (
                    lower_wick_atr >= wick_threshold
                    and close_position_bar >= float(params["min_close_position_long"])
                )
                short_proxy = (
                    upper_wick_atr >= wick_threshold
                    and close_position_bar <= float(params["max_close_position_short"])
                )
                if long_proxy:
                    candidate = min(
                        1.0,
                        0.25 + lower_wick_atr * 0.35 + (bar_volume - 1.0) * 0.12,
                    )
                    if abs(candidate) > abs(best_proxy_score):
                        best_proxy_score = candidate
                        best_lower_wick = lower_wick_atr
                        best_upper_wick = upper_wick_atr
                if short_proxy:
                    candidate = -min(
                        1.0,
                        0.25 + upper_wick_atr * 0.35 + (bar_volume - 1.0) * 0.12,
                    )
                    if abs(candidate) > abs(best_proxy_score):
                        best_proxy_score = candidate
                        best_lower_wick = lower_wick_atr
                        best_upper_wick = upper_wick_atr
            if best_proxy_score != 0.0:
                score = best_proxy_score
                source = "volume_wick_proxy"
            else:
                _reject(
                    prepared,
                    self.setup_id,
                    "liquidation_score_missing",
                    volume_ratio=vol_ratio,
                    lower_wick_atr=best_lower_wick,
                    upper_wick_atr=best_upper_wick,
                )
                return None
        else:
            source = "force_order"

        threshold = float(params["min_liquidation_score"])
        if score >= threshold and close_position >= float(params["min_close_position_long"]):
            direction = "long"
        elif score <= -threshold and close_position <= float(params["max_close_position_short"]):
            direction = "short"
        else:
            _reject(
                prepared,
                self.setup_id,
                "liquidation_cluster_not_actionable",
                liquidation_score=score,
            )
            return None
        context_penalty = _confirmed_context_conflict(prepared, direction)
        clarity = min(abs(score), 1.0)
        if source != "force_order":
            clarity *= 0.75
        if volume_penalty:
            clarity *= 0.90
        if context_penalty:
            clarity *= 0.82
        return _build_atr_signal(
            prepared=prepared,
            setup_id=self.setup_id,
            direction=direction,
            params=params,
            reasons=[
                f"liquidation_heatmap_{direction}",
                f"source={source}",
                f"liq_score={score:.2f}",
                f"volume_ratio={vol_ratio:.2f}",
            ],
            family=self.family,
            structure_clarity=clarity,
        )


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


class MultiTFTrendSetup(RoadmapSetup):
    setup_id = "multi_tf_trend"
    family = "continuation"
    confirmation_profile = "trend_follow"
    required_context = ("futures_flow",)
    DEFAULTS = {
        **RoadmapSetup.DEFAULTS,
        "min_adx_1h": 15.0,
        "min_volume_ratio": 0.90,
        "pullback_rsi_long_max": 58.0,
        "pullback_rsi_short_min": 42.0,
        "max_adverse_depth_imbalance": 1.00,
        "min_trend_votes": 2,
    }

    def detect(self, prepared: PreparedSymbol, settings: BotSettings) -> Signal | None:
        params = self._params(prepared, settings)
        adx_1h = _last(prepared.work_1h, "adx14")
        vol_ratio = _last(prepared.work_15m, "volume_ratio20", 1.0)
        rsi_15m = _last(prepared.work_15m, "rsi14", 50.0)
        if adx_1h < float(params["min_adx_1h"]):
            _reject(prepared, self.setup_id, "adx_too_low", adx_1h=adx_1h)
            return None
        if vol_ratio < float(params["min_volume_ratio"]):
            _reject(prepared, self.setup_id, "volume_too_low", volume_ratio=vol_ratio)
            return None
        context_values = [
            prepared.bias_4h,
            prepared.bias_1h,
            prepared.regime_4h_confirmed,
            prepared.regime_1h_confirmed,
        ]
        up_votes = sum(1 for value in context_values if value == "uptrend")
        down_votes = sum(1 for value in context_values if value == "downtrend")
        min_votes = int(params.get("min_trend_votes", 3))
        if up_votes >= min_votes and up_votes > down_votes:
            direction = "long"
        elif down_votes >= min_votes and down_votes > up_votes:
            direction = "short"
        else:
            _reject(
                prepared,
                self.setup_id,
                "multi_tf_not_aligned",
                up_votes=up_votes,
                down_votes=down_votes,
                min_votes=min_votes,
            )
            return None
        if direction == "long" and rsi_15m > float(params["pullback_rsi_long_max"]):
            _reject(
                prepared,
                self.setup_id,
                "pullback_quality_missing",
                direction=direction,
                rsi_15m=rsi_15m,
                max_rsi=float(params["pullback_rsi_long_max"]),
            )
            return None
        if direction == "short" and rsi_15m < float(params["pullback_rsi_short_min"]):
            _reject(
                prepared,
                self.setup_id,
                "pullback_quality_missing",
                direction=direction,
                rsi_15m=rsi_15m,
                min_rsi=float(params["pullback_rsi_short_min"]),
            )
            return None
        depth = _finite_or_none(prepared.depth_imbalance)
        max_adverse_depth = float(params.get("max_adverse_depth_imbalance", 1.00))
        if direction == "long" and depth is not None and depth <= -max_adverse_depth:
            _reject(
                prepared,
                self.setup_id,
                "orderflow_against_trend_pullback",
                depth_imbalance=depth,
            )
            return None
        if direction == "short" and depth is not None and depth >= max_adverse_depth:
            _reject(
                prepared,
                self.setup_id,
                "orderflow_against_trend_pullback",
                depth_imbalance=depth,
            )
            return None
        return _build_atr_signal(
            prepared=prepared,
            setup_id=self.setup_id,
            direction=direction,
            params=params,
            reasons=[
                f"multi_tf_pullback_{direction}",
                f"adx_1h={adx_1h:.1f}",
                f"rsi15={rsi_15m:.1f}",
                f"votes_up={up_votes} votes_down={down_votes}",
            ],
            family=self.family,
            structure_clarity=0.85,
        )


class RSIDivergenceBottomSetup(RoadmapSetup):
    setup_id = "rsi_divergence_bottom"
    family = "reversal"
    confirmation_profile = "countertrend_exhaustion"
    required_context = ("futures_flow",)
    DEFAULTS = {
        **RoadmapSetup.DEFAULTS,
        "divergence_window": 12,
        "min_rsi_delta": 3.0,
        "min_price_delta_pct": 0.10,
    }

    def detect(self, prepared: PreparedSymbol, settings: BotSettings) -> Signal | None:
        params = self._params(prepared, settings)
        work = prepared.work_15m
        missing = _missing_columns(work, ("high", "low", "close", "rsi14"))
        if missing:
            _reject(prepared, self.setup_id, "missing_columns", missing_fields=missing)
            return None
        window = int(params["divergence_window"])
        if work.height < window * 2:
            _reject(prepared, self.setup_id, "insufficient_divergence_window")
            return None
        previous = work.slice(work.height - window * 2, window)
        recent = work.tail(window)
        prev_low = _as_float(previous["low"].min())
        recent_low = _as_float(recent["low"].min())
        prev_high = _as_float(previous["high"].max())
        recent_high = _as_float(recent["high"].max())
        prev_rsi_low = _as_float(previous["rsi14"].min(), 50.0)
        recent_rsi_low = _as_float(recent["rsi14"].min(), 50.0)
        prev_rsi_high = _as_float(previous["rsi14"].max(), 50.0)
        recent_rsi_high = _as_float(recent["rsi14"].max(), 50.0)
        price_delta = float(params["min_price_delta_pct"]) / 100.0
        rsi_delta = float(params["min_rsi_delta"])
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
            _reject(prepared, self.setup_id, "rsi_divergence_missing")
            return None
        return _build_atr_signal(
            prepared=prepared,
            setup_id=self.setup_id,
            direction=direction,
            params=params,
            reasons=[
                f"rsi_divergence_{direction}",
                f"recent_rsi={_last(work, 'rsi14', 50.0):.1f}",
            ],
            family=self.family,
            structure_clarity=0.7,
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


class BBSqueezeSetup(RoadmapSetup):
    setup_id = "bb_squeeze"
    family = "volatility"
    confirmation_profile = "breakout_acceptance"
    required_context = ("futures_flow",)
    DEFAULTS = {
        **RoadmapSetup.DEFAULTS,
        "max_bb_width": 5.0,
        "min_volume_ratio": 0.90,
        "min_roc10_abs_pct": 0.10,
        "squeeze_release_lookback": 8.0,
        "squeeze_memory_bars": 20.0,
    }

    def detect(self, prepared: PreparedSymbol, settings: BotSettings) -> Signal | None:
        params = self._params(prepared, settings)
        work = prepared.work_15m
        missing = _missing_columns(work, ("bb_width", "squeeze_on", "squeeze_off", "roc10"))
        if missing:
            _reject(prepared, self.setup_id, "missing_columns", missing_fields=missing)
            return None
        bb_width = _last(work, "bb_width")
        release_lookback = int(params["squeeze_release_lookback"])
        memory_bars = int(params.get("squeeze_memory_bars", 20))
        prior = work.head(max(0, work.height - 1))
        squeeze_recent = _series_max_tail(prior, "squeeze_on", memory_bars)
        squeeze_release_recent = _series_max_tail(work, "squeeze_off", release_lookback)
        roc10 = _last(work, "roc10")
        vol_ratio = _last(work, "volume_ratio20", 1.0)
        if squeeze_recent <= 0.0 and bb_width > float(params["max_bb_width"]):
            _reject(prepared, self.setup_id, "bb_squeeze_not_active", bb_width=bb_width)
            return None
        volume_penalty = vol_ratio < float(params["min_volume_ratio"])
        if squeeze_release_recent <= 0.0 and bb_width <= float(params["max_bb_width"]):
            squeeze_release_recent = 1.0
        if squeeze_release_recent <= 0.0:
            _reject(
                prepared,
                self.setup_id,
                "squeeze_breakout_unconfirmed",
                squeeze_release_recent=squeeze_release_recent,
                volume_ratio=vol_ratio,
                release_lookback=release_lookback,
            )
            return None
        if abs(roc10) < float(params["min_roc10_abs_pct"]):
            _reject(prepared, self.setup_id, "momentum_too_low", roc10=roc10)
            return None
        direction = "long" if roc10 > 0.0 else "short"
        clarity = min(abs(roc10), 1.0) * (0.90 if volume_penalty else 1.0)
        return _build_atr_signal(
            prepared=prepared,
            setup_id=self.setup_id,
            direction=direction,
            params=params,
            reasons=[
                f"bb_squeeze_{direction}",
                f"bb_width={bb_width:.2f}",
                f"release_recent={squeeze_release_recent:.0f}",
            ],
            family=self.family,
            structure_clarity=clarity,
        )


class ATRExpansionSetup(RoadmapSetup):
    setup_id = "atr_expansion"
    family = "volatility"
    confirmation_profile = "breakout_acceptance"
    required_context = ("futures_flow",)
    DEFAULTS = {
        **RoadmapSetup.DEFAULTS,
        "atr_mean_window": 20,
        "min_atr_expansion_ratio": 1.08,
        "min_body_atr": 0.25,
        "signal_lookback_bars": 6,
    }

    def detect(self, prepared: PreparedSymbol, settings: BotSettings) -> Signal | None:
        params = self._params(prepared, settings)
        work = prepared.work_15m
        missing = _missing_columns(work, ("open", "close", "atr14"))
        if missing:
            _reject(prepared, self.setup_id, "missing_columns", missing_fields=missing)
            return None
        lookback = max(1, int(params.get("signal_lookback_bars", 6)))
        recent = work.tail(min(lookback, work.height))
        atr = _last(work, "atr14")
        mean_atr = _series_mean_tail(work, "atr14", int(params["atr_mean_window"]))
        if atr <= 0.0 or mean_atr <= 0.0:
            _reject(prepared, self.setup_id, "atr_invalid", atr=atr, mean_atr=mean_atr)
            return None
        best_idx = recent.height - 1
        ratio = 0.0
        body_atr = 0.0
        for local_idx in range(recent.height):
            bar_atr = _as_float(recent.item(local_idx, "atr14"))
            if bar_atr <= 0.0:
                continue
            candidate_ratio = bar_atr / mean_atr
            candidate_body = abs(
                _as_float(recent.item(local_idx, "close"))
                - _as_float(recent.item(local_idx, "open"))
            ) / bar_atr
            if candidate_ratio + candidate_body > ratio + body_atr:
                ratio = candidate_ratio
                body_atr = candidate_body
                best_idx = local_idx
        if ratio < float(params["min_atr_expansion_ratio"]) or body_atr < float(
            params["min_body_atr"]
        ):
            _reject(prepared, self.setup_id, "atr_expansion_too_low", atr_ratio=ratio)
            return None
        signal_open = _as_float(recent.item(best_idx, "open"))
        signal_close = _as_float(recent.item(best_idx, "close"))
        direction = "long" if signal_close >= signal_open else "short"
        signal_lag = recent.height - 1 - best_idx
        return _build_atr_signal(
            prepared=prepared,
            setup_id=self.setup_id,
            direction=direction,
            params=params,
            reasons=[
                f"atr_expansion_{direction}",
                f"atr_ratio={ratio:.2f}",
                f"body_atr={body_atr:.2f}",
                f"signal_lag={signal_lag}",
            ],
            family=self.family,
            structure_clarity=min((ratio - 1.0) / 1.0, 1.0),
        )


class LSRatioExtremeSetup(RoadmapSetup):
    setup_id = "ls_ratio_extreme"
    family = "sentiment"
    confirmation_profile = "countertrend_exhaustion"
    required_context = ("futures_flow",)
    DEFAULTS = {
        **RoadmapSetup.DEFAULTS,
        "long_crowd_threshold": 1.75,
        "short_crowd_threshold": 0.65,
        "min_close_position_long": 0.58,
        "max_close_position_short": 0.42,
        "min_volume_ratio": 0.90,
        "max_adverse_depth_imbalance": 0.10,
        "max_adverse_microprice_bias": 0.10,
        "sl_buffer_atr": 0.85,
        "min_rr": 1.9,
    }

    def detect(self, prepared: PreparedSymbol, settings: BotSettings) -> Signal | None:
        params = self._params(prepared, settings)
        ratio = _first_finite(
            prepared.top_account_ls_ratio,
            prepared.ls_ratio,
            prepared.global_ls_ratio,
        )
        if ratio is None:
            _reject(prepared, self.setup_id, "ls_ratio_missing")
            return None
        ls_ratio = ratio
        if ls_ratio >= float(params["long_crowd_threshold"]):
            direction = "short"
        elif ls_ratio <= float(params["short_crowd_threshold"]):
            direction = "long"
        else:
            _reject(prepared, self.setup_id, "ls_ratio_not_extreme", ls_ratio=ls_ratio)
            return None
        work = prepared.work_15m
        close_position = _last(work, "close_position", 0.5)
        volume_ratio = _last(work, "volume_ratio20", 1.0)
        if volume_ratio < float(params["min_volume_ratio"]):
            volume_penalty = True
        else:
            volume_penalty = False
        if direction == "long":
            close_ok = close_position >= float(params["min_close_position_long"])
        else:
            close_ok = close_position <= float(params["max_close_position_short"])
        if not close_ok:
            _reject(
                prepared,
                self.setup_id,
                "ls_ratio_price_confirmation_missing",
                ls_ratio=ls_ratio,
                close_position=close_position,
                direction=direction,
            )
            return None

        depth = _finite_or_none(prepared.depth_imbalance)
        micro = _finite_or_none(prepared.microprice_bias)
        max_depth = float(params["max_adverse_depth_imbalance"])
        max_micro = float(params["max_adverse_microprice_bias"])
        if direction == "long":
            adverse_depth = depth is not None and depth < -max_depth
            adverse_micro = micro is not None and micro < -max_micro
        else:
            adverse_depth = depth is not None and depth > max_depth
            adverse_micro = micro is not None and micro > max_micro
        if adverse_depth or adverse_micro:
            orderbook_penalty = True
        else:
            orderbook_penalty = False
        context_penalty = False
        if _confirmed_context_conflict(prepared, direction):
            context_penalty = True
        reasons = [
            f"ls_ratio_extreme_{direction}",
            f"ls_ratio={ls_ratio:.2f}",
            f"close_position={close_position:.2f}",
            f"volume_ratio={volume_ratio:.2f}",
        ]
        if volume_penalty:
            reasons.append("volume_confirmation_penalty")
        if orderbook_penalty:
            reasons.append("orderbook_against_penalty")
        if context_penalty:
            reasons.append("context_conflict_penalty")
        score_multiplier = 1.0
        if volume_penalty:
            score_multiplier *= 0.90
        if orderbook_penalty:
            score_multiplier *= 0.86
        if context_penalty:
            score_multiplier *= 0.82
        return _build_atr_signal(
            prepared=prepared,
            setup_id=self.setup_id,
            direction=direction,
            params=params,
            reasons=reasons,
            family=self.family,
            structure_clarity=min(abs(ls_ratio - 1.0), 1.0) * score_multiplier,
        )


class OIDivergenceSetup(RoadmapSetup):
    setup_id = "oi_divergence"
    family = "sentiment"
    confirmation_profile = "countertrend_exhaustion"
    required_context = ("futures_flow",)
    DEFAULTS = {
        **RoadmapSetup.DEFAULTS,
        "min_abs_oi_change_pct": 0.01,
        "min_price_change_pct": 0.35,
    }

    def detect(self, prepared: PreparedSymbol, settings: BotSettings) -> Signal | None:
        params = self._params(prepared, settings)
        oi_change = _finite_or_none(prepared.oi_change_pct)
        if oi_change is None:
            _reject(prepared, self.setup_id, "oi_change_missing")
            return None
        price_change = _price_change_pct(prepared.work_15m, 8)
        if abs(oi_change) < float(params["min_abs_oi_change_pct"]) or abs(price_change) < float(
            params["min_price_change_pct"]
        ):
            _reject(
                prepared,
                self.setup_id,
                "oi_price_divergence_too_small",
                oi_change_pct=oi_change,
            )
            return None
        if oi_change > 0.0:
            direction = "long" if price_change > 0.0 else "short"
            oi_context = "oi_confirms_price"
        elif price_change > 0.0:
            direction = "short"
            oi_context = "price_up_oi_contracting"
        else:
            direction = "long"
            oi_context = "price_down_oi_contracting"
        return _build_atr_signal(
            prepared=prepared,
            setup_id=self.setup_id,
            direction=direction,
            params=params,
            reasons=[
                f"oi_divergence_{direction}",
                oi_context,
                f"oi_change={oi_change:.2f}",
                f"price_change={price_change:.2f}",
            ],
            family=self.family,
            structure_clarity=min(abs(oi_change) / 0.05, 1.0),
        )


class BTCCorrelationSetup(RoadmapSetup):
    setup_id = "btc_correlation"
    family = "multi_asset"
    confirmation_profile = "trend_follow"
    required_context = ("futures_flow",)
    DEFAULTS = {
        **RoadmapSetup.DEFAULTS,
        "min_roc10_abs_pct": 0.15,
        "min_volume_ratio": 0.70,
    }

    def detect(self, prepared: PreparedSymbol, settings: BotSettings) -> Signal | None:
        params = self._params(prepared, settings)
        if prepared.symbol == "BTCUSDT":
            _reject(prepared, self.setup_id, "benchmark_symbol")
            return None
        btc_bias = getattr(prepared, "btc_bias", None)
        if btc_bias is None or str(btc_bias).strip() == "":
            _reject(prepared, self.setup_id, "btc_context_missing")
            return None
        btc_phase = str(getattr(prepared, "btc_phase", "") or "").lower()
        if btc_bias not in {"uptrend", "downtrend", "bull", "bear"}:
            if btc_phase in {"markup", "accumulation"}:
                btc_bias = "bull"
            elif btc_phase in {"decline", "distribution"}:
                btc_bias = "bear"
            else:
                btc_bias = "neutral"
        vol_ratio = _last(prepared.work_15m, "volume_ratio20", 1.0)
        volume_penalty = vol_ratio < float(params["min_volume_ratio"])
        roc10 = _last(prepared.work_15m, "roc10", _price_change_pct(prepared.work_15m, 10))
        if abs(roc10) < float(params["min_roc10_abs_pct"]):
            _reject(prepared, self.setup_id, "momentum_too_low", roc10=roc10)
            return None
        if btc_bias in {"uptrend", "bull"} and prepared.bias_1h != "downtrend" and roc10 > 0.0:
            direction = "long"
        elif btc_bias in {"downtrend", "bear"} and prepared.bias_1h != "uptrend" and roc10 < 0.0:
            direction = "short"
        elif btc_bias == "neutral" and prepared.bias_1h == "uptrend" and roc10 > 0.0:
            direction = "long"
        elif btc_bias == "neutral" and prepared.bias_1h == "downtrend" and roc10 < 0.0:
            direction = "short"
        else:
            _reject(
                prepared,
                self.setup_id,
                "btc_correlation_not_aligned",
                btc_bias=btc_bias,
            )
            return None
        reasons = [
            f"btc_correlation_{direction}",
            f"btc_bias={btc_bias}",
            f"btc_phase={btc_phase or '-'}",
            f"roc10={roc10:.2f}",
        ]
        if volume_penalty:
            reasons.append(f"volume_penalty={vol_ratio:.2f}")
        return _build_atr_signal(
            prepared=prepared,
            setup_id=self.setup_id,
            direction=direction,
            params=params,
            reasons=reasons,
            family=self.family,
            structure_clarity=0.65 if volume_penalty else 0.75,
        )


class AltcoinSeasonIndexSetup(RoadmapSetup):
    setup_id = "altcoin_season_index"
    family = "multi_asset"
    confirmation_profile = "trend_follow"
    required_context = ("futures_flow",)
    DEFAULTS = {
        **RoadmapSetup.DEFAULTS,
        "altseason_long_threshold": 55.0,
        "btc_dominance_threshold": 45.0,
        "min_volume_ratio": 0.80,
        "min_roc10_abs_pct": 0.10,
    }

    def detect(self, prepared: PreparedSymbol, settings: BotSettings) -> Signal | None:
        params = self._params(prepared, settings)
        base = str(prepared.universe.base_asset or "").upper()
        if not base:
            _reject(prepared, self.setup_id, "base_asset_missing")
            return None
        if base in {"BTC", "XAU", "XAG"}:
            _reject(prepared, self.setup_id, "not_altcoin")
            return None
        index = _finite_or_none(getattr(prepared, "altcoin_season_index", None))
        if index is None:
            _reject(prepared, self.setup_id, "altcoin_season_index_missing")
            return None
        alt_index = index
        vol_ratio = _last(prepared.work_15m, "volume_ratio20", 1.0)
        volume_penalty = vol_ratio < float(params["min_volume_ratio"])
        roc10 = _last(prepared.work_15m, "roc10", _price_change_pct(prepared.work_15m, 10))
        if (
            alt_index >= float(params["altseason_long_threshold"])
            and prepared.bias_1h != "downtrend"
        ):
            direction = "long"
        elif (
            alt_index <= float(params["btc_dominance_threshold"]) and prepared.bias_1h != "uptrend"
        ):
            direction = "short"
        elif abs(roc10) >= float(params["min_roc10_abs_pct"]) and prepared.bias_1h in {
            "uptrend",
            "downtrend",
        }:
            direction = "long" if prepared.bias_1h == "uptrend" else "short"
        else:
            _reject(
                prepared,
                self.setup_id,
                "altcoin_phase_not_actionable",
                altcoin_season_index=alt_index,
            )
            return None
        return _build_atr_signal(
            prepared=prepared,
            setup_id=self.setup_id,
            direction=direction,
            params=params,
            reasons=[
                f"altcoin_season_{direction}",
                f"alt_index={alt_index:.1f}",
                f"roc10={roc10:.2f}",
            ],
            family=self.family,
            structure_clarity=max(abs(alt_index - 50.0) / 50.0, min(abs(roc10), 1.0))
            * (0.90 if volume_penalty else 1.0),
        )
