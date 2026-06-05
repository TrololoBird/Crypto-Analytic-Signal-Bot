"""Roadmap detector helpers for public-data signal generation.

These setups are signal-only detectors. They intentionally use fields already
available in ``PreparedSymbol`` or prepared Polars frames; they do not call
exchange APIs and they do not place orders.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..setups import _build_signal, _compute_dynamic_score, _reject
from ._common import (
    as_float as _as_float,
)
from ._common import (
    finite_or_none as _finite_or_none,
)
from ._common import (
    first_finite as _first_finite,
)
from ._common import (
    last as _last,
)
from ._common import (
    previous as _prev,
)

if TYPE_CHECKING:
    import polars as pl

    from ..domain.config import BotSettings
    from ..domain.schemas import PreparedSymbol, Signal


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


def _price_change_pct_confirmed(frame: pl.DataFrame, bars: int = 8) -> float:
    """ROC ending on the last fully closed bar (df[-2]), not a forming tail."""
    if frame.height < 3 or "close" not in frame.columns:
        return 0.0
    end_idx = frame.height - 2
    anchor_idx = max(0, end_idx - max(2, bars))
    start = _as_float(frame.item(anchor_idx, "close"))
    end = _as_float(frame.item(end_idx, "close"))
    if start <= 0.0 or end <= 0.0:
        return 0.0
    return (end / start - 1.0) * 100.0


def _flow_delta_with_source(prepared: PreparedSymbol) -> tuple[float | None, str | None]:
    direct_delta = _first_finite(
        prepared.agg_trade_delta_30s,
        prepared.aggression_shift,
    )
    if direct_delta is not None:
        return direct_delta, str(getattr(prepared, "orderflow_source", None) or "agg_trade")
    taker_ratio = _finite_or_none(prepared.taker_ratio)
    if taker_ratio is not None:
        # clip to valid signed range; raw ratio can exceed bounds on thin books
        return float(max(-1.0, min(1.0, taker_ratio - 1.0))), "taker_ratio_rest"
    work = prepared.work_15m
    if work.is_empty() or "delta_ratio" not in work.columns:
        return None, None
    return _last(work, "delta_ratio", 0.5) - 0.5, "ohlcv_delta_proxy"


def _flow_delta(prepared: PreparedSymbol) -> float | None:
    value, _source = _flow_delta_with_source(prepared)
    return value


def _has_l2_depth(prepared: PreparedSymbol) -> bool:
    flags = set(getattr(prepared, "data_freshness_flags", ()) or ())
    return getattr(prepared, "depth_imbalance_source", None) == "l2_depth" and (
        "depth_book_stale" not in flags
    )


def _orderbook_source(prepared: PreparedSymbol) -> str:
    return str(getattr(prepared, "depth_imbalance_source", None) or "unknown")


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
    entry_anchor: float | None = None,
    stop_anchor: float | None = None,
    confirmed_bar: bool = False,
) -> Signal | None:
    work = prepared.work_15m
    # fix-sl-A: optional confirmed bar avoids anchoring entry on a forming candle tail.
    bar_pick = _prev if confirmed_bar else _last
    if confirmed_bar and work.height < 2:
        _reject(prepared, setup_id, "insufficient_closed_bars")
        return None
    close = bar_pick(work, "close")
    high = bar_pick(work, "high")
    low = bar_pick(work, "low")
    atr = bar_pick(work, "atr14")
    vol_ratio = bar_pick(work, "volume_ratio20", 1.0)
    rsi = bar_pick(work, "rsi14", 50.0)
    if min(close, high, low, atr) <= 0.0:
        _reject(prepared, setup_id, "invalid_indicator_state", close=close, atr=atr)
        return None

    sl_buffer = float(params.get("sl_buffer_atr", 0.65))
    min_rr = float(params.get("min_rr", 1.5))
    candle_mid = (high + low) / 2.0
    if entry_anchor is not None and entry_anchor > 0.0:
        price_anchor = float(entry_anchor)
    elif direction == "long":
        price_anchor = min(candle_mid, close)
    else:
        price_anchor = max(candle_mid, close)
    max_risk_atr = float(params.get("max_risk_atr", 2.0))
    max_risk_pct = float(params.get("max_risk_pct", 3.5))
    max_risk = min(atr * max_risk_atr, price_anchor * max_risk_pct / 100.0)
    if direction == "long":
        sweep_stop = stop_anchor if stop_anchor is not None and stop_anchor > 0.0 else low
        stop = min(sweep_stop, close - atr * sl_buffer) - atr * 0.05
        risk = price_anchor - stop
        if risk > max_risk:
            stop = price_anchor - max_risk
            risk = max_risk
        tp1 = price_anchor + risk * min_rr
        tp2 = price_anchor + risk * max(min_rr + 0.4, 2.0)
    else:
        sweep_stop = stop_anchor if stop_anchor is not None and stop_anchor > 0.0 else high
        stop = max(sweep_stop, close + atr * sl_buffer) + atr * 0.05
        risk = stop - price_anchor
        if risk > max_risk:
            stop = price_anchor + max_risk
            risk = max_risk
        tp1 = price_anchor - risk * min_rr
        tp2 = price_anchor - risk * max(min_rr + 0.4, 2.0)
    if risk <= 0.0:
        _reject(prepared, setup_id, "invalid_stop", stop=stop, close=price_anchor)
        return None

    score = _compute_dynamic_score(
        direction=direction,
        base_score=float(params.get("base_score", 0.52)),
        vol_ratio=vol_ratio,
        rsi=rsi,
        structure_clarity=max(0.0, min(1.0, structure_clarity)),
    )
    # floor: no signal delivered below 0.38 after penalties
    score = max(0.38, round(score, 4))
    return _build_signal(
        prepared=prepared,
        setup_id=setup_id,
        direction=direction,
        score=score,
        timeframe=timeframe,
        reasons=[*reasons, f"limit_entry={price_anchor:.4f}"],
        strategy_family=family,
        stop=stop,
        tp1=tp1,
        tp2=tp2,
        price_anchor=price_anchor,
        atr=atr,
    )


__all__ = [
    "_as_float",
    "_build_atr_signal",
    "_configured_params",
    "_confirmed_context_conflict",
    "_finite_or_none",
    "_first_finite",
    "_flow_delta",
    "_flow_delta_with_source",
    "_has_l2_depth",
    "_last",
    "_missing_columns",
    "_orderbook_source",
    "_prev",
    "_price_change_pct",
    "_price_change_pct_confirmed",
    "_reject",
    "_series_max_tail",
    "_series_mean_tail",
    "_series_min_tail",
]
