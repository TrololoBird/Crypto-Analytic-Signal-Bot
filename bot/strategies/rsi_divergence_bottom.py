from __future__ import annotations

from typing import TYPE_CHECKING

from engine.domain.strategy_catalog import catalog_default_params

from ._common import build_spec_signal
from ._roadmap import _as_float, _build_atr_signal, _missing_columns, _reject
from .indicator_divergence import detect_regular_divergence

if TYPE_CHECKING:
    from engine.domain.config import BotSettings
    from engine.domain.schemas import PreparedSymbol, Signal

__all__ = ["detect_rsi_divergence_bottom"]


def detect_rsi_divergence_bottom(
    prepared: PreparedSymbol,
    settings: BotSettings,
    effective_params: dict[str, float],
    *,
    setup_id: str,
    family: str,
) -> Signal | None:
    params = effective_params
    work = prepared.work_1h
    hit = detect_regular_divergence(
        work,
        timeframe="1h",
        setup_id=setup_id,
        require_oversold=True,
    )
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

    missing = _missing_columns(work, ("open", "high", "low", "close", "atr14", "rsi14"))
    if missing:
        _reject(prepared, setup_id, "missing_columns", missing_fields=missing)
        return None
    window = int(params["divergence_window"])
    if work.height < window * 2:
        _reject(prepared, setup_id, "insufficient_divergence_window")
        return None
    previous = work.slice(work.height - window * 2, window)
    recent = work.tail(window)
    previous_lows = [_as_float(value) for value in previous["low"].to_list()]
    recent_lows = [_as_float(value) for value in recent["low"].to_list()]
    previous_highs = [_as_float(value) for value in previous["high"].to_list()]
    recent_highs = [_as_float(value) for value in recent["high"].to_list()]
    if not previous_lows or not recent_lows or not previous_highs or not recent_highs:
        _reject(prepared, setup_id, "rsi_divergence_missing")
        return None
    prev_low_idx = min(range(len(previous_lows)), key=previous_lows.__getitem__)
    recent_low_idx = min(range(len(recent_lows)), key=recent_lows.__getitem__)
    prev_high_idx = max(range(len(previous_highs)), key=previous_highs.__getitem__)
    recent_high_idx = max(range(len(recent_highs)), key=recent_highs.__getitem__)
    prev_low = previous_lows[prev_low_idx]
    recent_low = recent_lows[recent_low_idx]
    prev_high = previous_highs[prev_high_idx]
    recent_high = recent_highs[recent_high_idx]
    prev_rsi_low = _as_float(previous.item(prev_low_idx, "rsi14"), 50.0)
    recent_rsi_low = _as_float(recent.item(recent_low_idx, "rsi14"), 50.0)
    prev_rsi_high = _as_float(previous.item(prev_high_idx, "rsi14"), 50.0)
    recent_rsi_high = _as_float(recent.item(recent_high_idx, "rsi14"), 50.0)
    price_delta = float(params["min_price_delta_pct"]) / 100.0
    rsi_delta = float(params["min_rsi_delta"])
    current_rsi = _as_float(work.item(-1, "rsi14"), 50.0)
    close_position = (
        _as_float(work.item(-1, "close_position"), 0.5) if "close_position" in work.columns else 0.5
    )

    if recent_low < prev_low * (1.0 - price_delta) and recent_rsi_low >= prev_rsi_low + rsi_delta:
        direction = "long"
    elif (
        recent_high > prev_high * (1.0 + price_delta)
        and recent_rsi_high <= prev_rsi_high - rsi_delta
    ):
        direction = "short"
    else:
        _reject(
            prepared,
            setup_id,
            "rsi_divergence_missing",
            prev_low=prev_low,
            recent_low=recent_low,
            prev_rsi_low=prev_rsi_low,
            recent_rsi_low=recent_rsi_low,
            prev_high=prev_high,
            recent_high=recent_high,
            prev_rsi_high=prev_rsi_high,
            recent_rsi_high=recent_rsi_high,
            current_rsi=current_rsi,
        )
        return None

    return _build_atr_signal(
        prepared=prepared,
        setup_id=setup_id,
        direction=direction,
        params=params,
        confirmed_bar=True,
        reasons=[
            f"rsi_divergence_{direction}",
            f"price_low={prev_low:.4f}->{recent_low:.4f}",
            f"price_high={prev_high:.4f}->{recent_high:.4f}",
            (
                f"rsi_low={prev_rsi_low:.1f}->{recent_rsi_low:.1f} "
                f"rsi_high={prev_rsi_high:.1f}->{recent_rsi_high:.1f}"
            ),
            f"current_rsi={current_rsi:.1f}",
            f"close_position={close_position:.2f}",
        ],
        family=family,
        entry_anchor=recent_low if direction == "long" else recent_high,
    )
