"""Strategy lanes — limit detectors per symbol per kline event (target spec)."""

from __future__ import annotations

from collections.abc import Iterable

from bot.domain.config import BotSettings
from bot.domain.strategies import StrategyMetadata
from bot.engine.registry import StrategyRegistry
def select_lane_setups(
    registry: StrategyRegistry,
    *,
    symbol: str,
    interval: str,
    settings: BotSettings,
    strategy_fits: Iterable[str] | None = None,
    market_row: dict[str, object] | None = None,
) -> list[StrategyMetadata]:
    """Return metadata for setups to run on this symbol+interval."""
    runtime = settings.runtime
    max_families = int(getattr(runtime, "max_setup_families_per_symbol", 15) or 15)
    route_all = bool(getattr(runtime, "route_all_enabled_strategies", False))

    enabled = registry.list_enabled()
    if strategy_fits is not None:
        fit_set = set(strategy_fits)
        enabled = [m for m in enabled if m.strategy_id in fit_set]
    elif not route_all:
        if market_row is not None:
            from bot.market.universe import strategy_fits_for_market_row

            fits = strategy_fits_for_market_row(market_row)
            fit_set = set(fits)
            enabled = [m for m in enabled if m.strategy_id in fit_set]
        else:
            enabled = list(enabled)

    interval_matches = [m for m in enabled if interval in _intervals_for(m)]
    if not interval_matches:
        return []

    return interval_matches[:max_families]


def _intervals_for(meta: StrategyMetadata) -> tuple[str, ...]:
    primary = str(meta.trigger_tf or "15m")
    extra = getattr(meta, "trigger_intervals", None)
    if extra:
        return tuple({primary, *(str(x) for x in extra)})
    tfs = getattr(meta, "timeframes", None) or ()
    if tfs:
        return tuple(str(x) for x in tfs)
    return (primary,)
