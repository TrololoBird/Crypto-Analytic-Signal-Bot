"""Roadmap strategy base - params wiring; detect logic in bot/strategies/."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from ..setups.base import BaseSetup
from ..setups.utils import get_dynamic_params
from ._roadmap import (
    _as_float,
    _build_atr_signal,
    _configured_params,
    _confirmed_context_conflict,
    _finite_or_none,
    _first_finite,
    _flow_delta,
    _flow_delta_with_source,
    _has_l2_depth,
    _last,
    _missing_columns,
    _orderbook_source,
    _prev,
    _price_change_pct,
    _reject,
    _series_max_tail,
    _series_mean_tail,
    _series_min_tail,
)

if TYPE_CHECKING:
    from ..domain.config import BotSettings
    from ..domain.schemas import PreparedSymbol


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


__all__ = [
    "RoadmapSetup",
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
    "_reject",
    "_series_max_tail",
    "_series_mean_tail",
    "_series_min_tail",
]
