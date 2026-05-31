"""liquidation_heatmap detector."""
from __future__ import annotations

from ...domain.config import BotSettings
from ...domain.schemas import PreparedSymbol, Signal
from ._roadmap import (
    _as_float,
    _build_atr_signal,
    _confirmed_context_conflict,
    _finite_or_none,
    _last,
    _reject,
)

__all__ = ["detect_liquidation_heatmap"]


def detect_liquidation_heatmap(
    prepared: PreparedSymbol,
    settings: BotSettings,
    effective_params: dict[str, float],
    *,
    setup_id: str,
    family: str,
) -> Signal | None:
    params = effective_params
    score = _finite_or_none(prepared.liquidation_score)
    source = str(getattr(prepared, "liquidation_score_source", None) or "missing")
    oi_change = _finite_or_none(prepared.oi_change_pct)
    close_position = _last(prepared.work_15m, "close_position", 0.5)
    vol_ratio = _last(prepared.work_15m, "volume_ratio20", 1.0)
    volume_penalty = vol_ratio < float(params["min_volume_ratio"])
    if score is None and oi_change is not None and oi_change <= -float(params["min_oi_drop_pct"]):
        price_change = _last(prepared.work_15m, "roc10", 0.0)
        score = 1.0 if price_change >= 0.0 else -1.0
        source = "oi_drop_proxy"
    threshold = float(params["min_liquidation_score"])
    if score is None:
        atr = _last(prepared.work_15m, "atr14")
        recent = prepared.work_15m.tail(
            min(int(params.get("proxy_lookback_bars", 12)), prepared.work_15m.height)
        )
        min_proxy_volume = float(params["min_proxy_volume_ratio"])
        min_proxy_wick = float(params["min_proxy_wick_atr"])
        for local_idx in range(recent.height - 1, -1, -1):
            if atr <= 0.0:
                break
            open_ = _as_float(recent.item(local_idx, "open"))
            high = _as_float(recent.item(local_idx, "high"))
            low = _as_float(recent.item(local_idx, "low"))
            bar_close = _as_float(recent.item(local_idx, "close"))
            bar_volume = _as_float(recent.item(local_idx, "volume_ratio20"), 1.0)
            bar_close_position = _as_float(recent.item(local_idx, "close_position"), 0.5)
            lower_wick_atr = (min(open_, bar_close) - low) / atr
            upper_wick_atr = (high - max(open_, bar_close)) / atr
            if (
                bar_volume >= min_proxy_volume
                and lower_wick_atr >= min_proxy_wick
                and bar_close_position >= float(params["min_close_position_long"])
            ):
                score = max(threshold, min(1.0, lower_wick_atr / 1.5))
                source = "volume_wick_proxy"
                close_position = max(close_position, bar_close_position)
                break
            if (
                bar_volume >= min_proxy_volume
                and upper_wick_atr >= min_proxy_wick
                and bar_close_position <= float(params["max_close_position_short"])
            ):
                score = -max(threshold, min(1.0, upper_wick_atr / 1.5))
                source = "volume_wick_proxy"
                close_position = min(close_position, bar_close_position)
                break
    if score is None:
        _reject(
            prepared,
            setup_id,
            "data.liquidation_score_missing",
            liquidation_source=source,
            oi_change_pct=oi_change,
            volume_ratio=vol_ratio,
        )
        return None
    if source not in {"force_order", "oi_drop_proxy", "volume_wick_proxy"}:
        _reject(
            prepared,
            setup_id,
            "data.liquidation_score_missing",
            liquidation_source=source,
            oi_change_pct=oi_change,
            volume_ratio=vol_ratio,
        )
        return None

    if score >= threshold and close_position >= float(params["min_close_position_long"]):
        direction = "long"
    elif score <= -threshold and close_position <= float(params["max_close_position_short"]):
        direction = "short"
    else:
        _reject(
            prepared,
            setup_id,
            "data.liquidation_score_missing",
            liquidation_score=score,
            oi_change_pct=oi_change,
        )
        return None
    context_penalty = _confirmed_context_conflict(prepared, direction)
    clarity = min(abs(score), 1.0)
    if source == "oi_drop_proxy":
        clarity *= 0.82
    if source == "volume_wick_proxy":
        clarity *= 0.62
    if volume_penalty:
        clarity *= 0.90
    if context_penalty:
        clarity *= 0.82
    return _build_atr_signal(
        prepared=prepared,
        setup_id=setup_id,
        direction=direction,
        params=params,
        reasons=[
            f"liquidation_heatmap_{direction}",
            f"source={source}",
            f"liq_score={score:.2f}",
            f"volume_ratio={vol_ratio:.2f}",
        ],
        family=family,
        structure_clarity=clarity,
    )
