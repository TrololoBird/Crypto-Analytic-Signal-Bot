from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from ..domain.strategy_catalog import catalog_default_params
from ._common import (
    SpecHit,
    _latest_values,
    as_float,
    build_spec_signal,
    finite_or_none,
    with_spec_columns,
)
from ._roadmap import _build_atr_signal, _flow_delta_with_source, _last, _prev, _reject
from .roadmap_base import RoadmapSetup

if TYPE_CHECKING:
    import polars as pl

    from ..domain.config import BotSettings
    from ..domain.schemas import PreparedSymbol, Signal

__all__ = ["detect_absorption", "detect_absorption_prepared"]


def detect_absorption(frame: pl.DataFrame, *, timeframe: str = "15m") -> SpecHit | None:
    work = with_spec_columns(frame)
    if work.height < 26:
        return None
    prev = work.row(-2, named=True)
    latest = _latest_values(work)
    delta = finite_or_none(prev.get("spec_delta"))
    delta_mean = finite_or_none(prev.get("spec_abs_delta_mean20"))
    threshold_mult = 2.0
    if delta is None:
        return None
    if delta_mean is None:
        return None
    atr = as_float(prev.get("spec_atr14"), latest.get("spec_atr14", 0.0))
    body = as_float(prev.get("spec_body"))
    if atr <= 0.0 or delta_mean <= 0.0:
        return None
    absorbed = abs(delta) > delta_mean * threshold_mult and body < atr * 0.4
    if not absorbed:
        return None
    prev_close = as_float(prev.get("close"))
    if delta < 0.0 and latest["close"] > prev_close:
        return SpecHit(
            strategy="absorption",
            direction="long",
            entry=prev_close,
            stop_basis=as_float(prev.get("low")),
            atr=atr,
            timeframe=timeframe,
            reasons=(
                f"sell_delta_absorbed delta_x={abs(delta) / delta_mean:.2f}",
                "delta_source=spec_delta",
            ),
            structure_clarity=min(1.0, abs(delta) / max(delta_mean * 4.0, 1e-8)),
            vol_ratio=latest.get("volume_ratio20", 1.0),
            rsi=latest.get("rsi14", 50.0),
        )
    if delta > 0.0 and latest["close"] < prev_close:
        return SpecHit(
            strategy="absorption",
            direction="short",
            entry=prev_close,
            stop_basis=as_float(prev.get("high")),
            atr=atr,
            timeframe=timeframe,
            reasons=(
                f"buy_delta_absorbed delta_x={abs(delta) / delta_mean:.2f}",
                "delta_source=spec_delta",
            ),
            structure_clarity=min(1.0, abs(delta) / max(delta_mean * 4.0, 1e-8)),
            vol_ratio=latest.get("volume_ratio20", 1.0),
            rsi=latest.get("rsi14", 50.0),
        )
    return None


def detect_absorption_prepared(
    prepared: PreparedSymbol,
    settings: BotSettings,
    effective_params: dict[str, float],
    *,
    setup_id: str,
    family: str,
) -> Signal | None:
    params = effective_params
    hit = detect_absorption(prepared.work_15m, timeframe="15m")
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

    # FIX 2026-05-21: a strict spec miss must not bypass the configured
    # orderflow/candle absorption fallback below.
    if prepared.agg_trade_delta_30s is None:
        _reject(prepared, setup_id, "orderflow_missing")
        return None
    flow, flow_source = _flow_delta_with_source(prepared)
    if flow is None:
        _reject(prepared, setup_id, "orderflow_delta_missing")
        return None
    work = prepared.work_15m
    if work.height < 2:
        _reject(prepared, setup_id, "insufficient_bars")
        return None
    close_position = _prev(work, "close_position", 0.5)
    atr = _prev(work, "atr14")
    high = _prev(work, "high")
    low = _prev(work, "low")
    close = _prev(work, "close")
    open_ = _prev(work, "open")
    if min(atr, high, low, close, open_) <= 0.0:
        _reject(prepared, setup_id, "invalid_indicator_state", atr=atr)
        return None
    lower_wick_atr = (min(open_, close) - low) / atr
    upper_wick_atr = (high - max(open_, close)) / atr
    vol_ratio = _prev(work, "volume_ratio20", 1.0)
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
        _reject(prepared, setup_id, "absorption_not_confirmed", flow_delta=flow)
        return None
    clarity = min(abs(flow) * 2.0, 1.0)
    if flow_source != "agg_trade":
        clarity *= 0.72 if flow_source == "ohlcv_delta_proxy" else 0.85
    if volume_penalty:
        clarity *= 0.90
    return _build_atr_signal(
        prepared=prepared,
        setup_id=setup_id,
        direction=direction,
        params=params,
        reasons=[
            f"absorption_{direction}",
            f"flow_delta={flow:.3f}",
            f"flow_source={flow_source}",
            f"close_position={close_position:.2f}",
            f"volume_ratio={vol_ratio:.2f}",
        ],
        family=family,
        structure_clarity=clarity,
        confirmed_bar=True,
    )


class AbsorptionSetup(RoadmapSetup):
    setup_id = "absorption"
    family = "orderflow"
    confirmation_profile = "countertrend_exhaustion"
    required_context = ("futures_flow",)
    DEFAULTS: ClassVar[dict[str, float]] = {
        **RoadmapSetup.DEFAULTS,
        "min_abs_flow_delta": 0.05,
        "min_close_position_long": 0.55,
        "max_close_position_short": 0.45,
        "min_wick_atr": 0.12,
        "min_volume_ratio": 0.90,
    }

    def detect(self, prepared: PreparedSymbol, settings: BotSettings) -> Signal | None:
        return detect_absorption_prepared(
            prepared,
            settings,
            self._params(prepared, settings),
            setup_id=self.setup_id,
            family=self.family,
        )


__all__ = ["AbsorptionSetup"]
