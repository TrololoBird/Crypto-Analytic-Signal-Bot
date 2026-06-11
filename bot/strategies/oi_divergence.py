from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from ._roadmap import (
    _build_atr_signal,
    _finite_or_none,
    _prev,
    _price_change_pct,
    _price_change_pct_confirmed,
    _reject,
)
from .roadmap_base import RoadmapSetup

if TYPE_CHECKING:
    from engine.domain.config import BotSettings
    from engine.domain.schemas import PreparedSymbol, Signal

__all__ = ["detect_oi_divergence"]


def _oi_divergence_price_change(prepared: PreparedSymbol, *, fallback_bars: int = 4) -> float:
    """Evaluate price change on 1h entry frame (answers.md Part 3: oi_divergence @ 1h)."""
    frame_1h = prepared.work_1h
    if frame_1h is not None and frame_1h.height >= 2 and "close" in frame_1h.columns:
        return _price_change_pct(frame_1h, bars=1)
    return _price_change_pct_confirmed(prepared.work_15m, fallback_bars)


def detect_oi_divergence(
    prepared: PreparedSymbol,
    _settings: BotSettings,
    effective_params: dict[str, float],
    *,
    setup_id: str,
    family: str,
) -> Signal | None:
    params = effective_params
    oi_change = _finite_or_none(prepared.oi_change_pct)
    if oi_change is None:
        _reject(prepared, setup_id, "asset_fit.oi_missing")
        return None
    price_change = _oi_divergence_price_change(prepared)
    if abs(oi_change) < float(params["min_abs_oi_change_pct"]) or abs(price_change) < float(
        params["min_price_change_pct"]
    ):
        _reject(
            prepared,
            setup_id,
            "indicator.oi_price_divergence_too_small",
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
    work = prepared.work_1h
    # Limit order: sell into prev-bar high (resistance) for shorts, buy at prev-bar low
    # (support) for longs — EMA20 ≈ current price yields immediate market-fill.
    if direction == "long":
        entry_anchor = _prev(work, "low", 0.0) or None
    else:
        entry_anchor = _prev(work, "high", 0.0) or None
    return _build_atr_signal(
        prepared=prepared,
        setup_id=setup_id,
        direction=direction,
        params=params,
        reasons=[
            f"oi_divergence_{direction}",
            oi_context,
            f"oi_change={oi_change:.2f}",
            f"price_change={price_change:.2f}",
        ],
        family=family,
        entry_anchor=entry_anchor,
        structure_clarity=min(abs(oi_change) / 0.05, 1.0),
        confirmed_bar=True,
    )


class OIDivergenceSetup(RoadmapSetup):
    setup_id = "oi_divergence"
    ENTRY_ORDER_TYPE: ClassVar[str] = "market"
    family = "sentiment"
    confirmation_profile = "countertrend_exhaustion"
    required_context = ("futures_flow",)
    DEFAULTS: ClassVar[dict[str, float]] = {
        **RoadmapSetup.DEFAULTS,
        "min_abs_oi_change_pct": 0.005,
        "min_price_change_pct": 0.06,
    }

    def detect(self, prepared: PreparedSymbol, settings: BotSettings) -> Signal | None:
        return detect_oi_divergence(
            prepared,
            settings,
            self._params(prepared, settings),
            setup_id=self.setup_id,
            family=self.family,
        )


__all__ = ["OIDivergenceSetup"]
