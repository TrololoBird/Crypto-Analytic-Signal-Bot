from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from ._roadmap import (
    _as_float,
    _build_atr_signal,
    _missing_columns,
    _prev,
    _reject,
)
from .roadmap_base import RoadmapSetup

if TYPE_CHECKING:
    from ..domain.config import BotSettings
    from ..domain.schemas import PreparedSymbol, Signal

__all__ = ["_current_expansion_candidate", "detect_atr_expansion_prepared"]


def _current_expansion_candidate(
    work,
    params: dict[str, float],
    *,
    timeframe: str,
) -> dict[str, float | int | str] | None:
    missing = _missing_columns(work, ("open", "high", "low", "close", "atr14"))
    if missing or work.height < 2:
        return None

    lookback = max(1, min(int(params.get("signal_lookback_bars", 1)), 12))
    start_idx = max(1, work.height - lookback)
    best: dict[str, float | int | str] | None = None
    min_ratio = float(params["min_atr_expansion_ratio"])
    min_body_atr = float(params["min_body_atr"])

    for idx in range(work.height - 2, start_idx - 1, -1):
        open_ = _as_float(work.item(idx, "open"))
        high = _as_float(work.item(idx, "high"))
        low = _as_float(work.item(idx, "low"))
        close = _as_float(work.item(idx, "close"))
        prev_close = _as_float(work.item(idx - 1, "close"))
        atr = _as_float(work.item(idx, "atr14"))
        if min(open_, high, low, close, prev_close, atr) <= 0.0:
            continue
        true_range = max(high - low, abs(high - prev_close), abs(low - prev_close))
        ratio = true_range / atr if atr > 0.0 else 0.0
        if ratio < min_ratio:
            continue
        body_atr = abs(close - open_) / max(atr, 1e-12)
        if body_atr < min_body_atr:
            continue
        candidate = {
            "score": ratio + body_atr,
            "ratio": ratio,
            "body_atr": body_atr,
            "direction": "long" if close >= open_ else "short",
            "signal_lag": work.height - 1 - idx,
            "timeframe": timeframe,
        }
        if best is None or float(candidate["score"]) > float(best["score"]):
            best = candidate
    return best


def detect_atr_expansion_prepared(
    prepared: PreparedSymbol,
    _settings: BotSettings,
    effective_params: dict[str, float],
    *,
    setup_id: str,
    family: str,
) -> Signal | None:
    params = effective_params
    work = prepared.work_15m
    missing = _missing_columns(work, ("open", "high", "low", "close", "atr14"))
    if missing:
        _reject(prepared, setup_id, "missing_columns", missing_fields=missing)
        return None

    candidate = _current_expansion_candidate(work, params, timeframe="15m")
    if candidate is None:
        _reject(
            prepared,
            setup_id,
            "indicator.atr_expansion_too_low",
            min_atr_expansion_ratio=float(params["min_atr_expansion_ratio"]),
            min_body_atr=float(params["min_body_atr"]),
        )
        return None
    direction = str(candidate["direction"])
    ratio = float(candidate["ratio"])
    body_atr = float(candidate["body_atr"])
    signal_lag = int(candidate["signal_lag"])
    source_timeframe = str(candidate["timeframe"])
    obv_penalty = 1.0
    if "obv_above_ema" in prepared.work_15m.columns:
        obv_val = float(prepared.work_15m["obv_above_ema"][-1] or 0.0)
        if (direction == "long" and obv_val <= 0.0) or (direction == "short" and obv_val > 0.0):
            obv_penalty = 0.85

    # Limit order: sell into prev-bar high (resistance) for shorts, buy at prev-bar low
    # (support) for longs — EMA20 ≈ current price yields immediate market-fill.
    if direction == "long":
        entry_anchor = _prev(prepared.work_15m, "low", 0.0) or None
    else:
        entry_anchor = _prev(prepared.work_15m, "high", 0.0) or None
    clarity = min((ratio - 1.0) / 1.0, 1.0) * obv_penalty
    reasons = [
        f"atr_expansion_{direction}",
        f"source_tf={source_timeframe}",
        f"atr_ratio={ratio:.2f}",
        f"body_atr={body_atr:.2f}",
        f"signal_lag={signal_lag}",
    ]
    if obv_penalty < 1.0:
        reasons.append("obv_opposes_breakout")
    return _build_atr_signal(
        prepared=prepared,
        setup_id=setup_id,
        direction=direction,
        params=params,
        confirmed_bar=True,
        reasons=reasons,
        family=family,
        entry_anchor=entry_anchor,
        timeframe=source_timeframe,
        structure_clarity=clarity,
    )


class ATRExpansionSetup(RoadmapSetup):
    setup_id = "atr_expansion"
    ENTRY_ORDER_TYPE: ClassVar[str] = "market"
    family = "volatility"
    confirmation_profile = "breakout_acceptance"
    required_context = ("futures_flow",)
    DEFAULTS: ClassVar[dict[str, float]] = {
        **RoadmapSetup.DEFAULTS,
        "atr_mean_window": 20,
        "min_atr_expansion_ratio": 1.75,
        "min_body_atr": 0.25,
        "signal_lookback_bars": 8,
    }

    def detect(self, prepared: PreparedSymbol, settings: BotSettings) -> Signal | None:
        return detect_atr_expansion_prepared(
            prepared,
            settings,
            self._params(prepared, settings),
            setup_id=self.setup_id,
            family=self.family,
        )


__all__ = ["ATRExpansionSetup"]
