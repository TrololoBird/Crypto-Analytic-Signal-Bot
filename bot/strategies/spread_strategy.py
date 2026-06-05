from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from ._roadmap import (
    _build_atr_signal,
    _confirmed_context_conflict,
    _finite_or_none,
    _orderbook_source,
    _prev,
    _price_change_pct_confirmed,
    _reject,
)
from .roadmap_base import RoadmapSetup

if TYPE_CHECKING:
    from ..domain.config import BotSettings
    from ..domain.schemas import PreparedSymbol, Signal

__all__ = ["detect_spread_strategy"]


def detect_spread_strategy(
    prepared: PreparedSymbol,
    _settings: BotSettings,
    effective_params: dict[str, float],
    *,
    setup_id: str,
    family: str,
) -> Signal | None:
    params = effective_params
    spread = _finite_or_none(prepared.spread_bps)
    if spread is None:
        _reject(prepared, setup_id, "data.spread_missing")
        return None
    if spread > float(params["max_spread_bps"]):
        _reject(prepared, setup_id, "spread_too_wide", spread_bps=spread)
        return None
    work = prepared.work_15m
    # fix-sl-A: confirm momentum on last closed bar (df[-2]), not forming tail.
    vol_ratio = _prev(work, "volume_ratio20", 1.0)
    roc10 = _prev(work, "roc10", _price_change_pct_confirmed(work, 10))
    volume_penalty = vol_ratio < float(params["min_volume_ratio"])
    if vol_ratio < 0.5:
        _reject(prepared, setup_id, "context.momentum_too_low", volume_ratio=vol_ratio)
        return None
    if abs(roc10) < float(params["min_roc10_abs_pct"]):
        _reject(prepared, setup_id, "context.momentum_too_low", roc10=roc10)
        return None
    direction = "long" if roc10 > 0.0 else "short"
    depth = _finite_or_none(prepared.depth_imbalance)
    micro = _finite_or_none(prepared.microprice_bias)
    if depth is None or micro is None:
        _reject(
            prepared,
            setup_id,
            "orderbook_context_incomplete",
            depth_imbalance=depth,
            microprice_bias=micro,
        )
        return None
    depth_source = _orderbook_source(prepared)
    if depth_source not in {"l2_depth", "l1_book", "rest_book_l1"}:
        _reject(
            prepared,
            setup_id,
            "orderbook_source_not_actionable",
            depth_source=depth_source,
            depth_imbalance=depth,
            microprice_bias=micro,
        )
        return None
    depth_value = depth
    micro_value = micro
    close_position = _prev(work, "close_position", 0.5)
    if direction == "long":
        orderbook_ok = depth_value >= float(params["min_depth_imbalance"]) and micro_value >= float(
            params["min_microprice_bias"]
        )
        close_ok = close_position >= float(params["min_close_position_long"])
    else:
        orderbook_ok = depth_value <= -float(
            params["min_depth_imbalance"]
        ) and micro_value <= -float(params["min_microprice_bias"])
        close_ok = close_position <= float(params["max_close_position_short"])
    if not orderbook_ok and not close_ok:
        _reject(
            prepared,
            setup_id,
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
        setup_id=setup_id,
        direction=direction,
        params=params,
        confirmed_bar=True,
        reasons=[
            f"tight_spread_{direction}",
            f"spread_bps={spread:.2f}",
            f"roc10={roc10:.2f}",
            f"depth={depth_value:.3f}",
            f"depth_source={depth_source}",
            f"micro={micro_value:.5f}",
            f"close_position={close_position:.2f}",
        ],
        family=family,
        structure_clarity=clarity,
    )


class SpreadStrategySetup(RoadmapSetup):
    setup_id = "spread_strategy"
    family = "orderbook"
    confirmation_profile = "breakout_acceptance"
    required_context = ("futures_flow",)
    DEFAULTS: ClassVar[dict[str, float]] = {
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
        return detect_spread_strategy(
            prepared,
            settings,
            self._params(prepared, settings),
            setup_id=self.setup_id,
            family=self.family,
        )


__all__ = ["SpreadStrategySetup"]
