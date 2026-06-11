"""Kline interval scheduling helpers."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bot.engine.registry import StrategyRegistry
    from engine.domain.config import BotSettings
    from engine.domain.strategies import StrategyMetadata


def analysis_intervals(settings: BotSettings) -> tuple[str, ...]:
    """Intervals that trigger symbol analysis on kline close."""
    raw = getattr(settings.runtime, "analysis_kline_intervals", None) or ("15m",)
    return tuple(dict.fromkeys(str(x) for x in raw))


def strategy_intervals(settings: BotSettings, registry: StrategyRegistry | None) -> tuple[str, ...]:
    """Union of strategy-declared trigger/context intervals with safe runtime fallback."""
    if registry is None:
        return analysis_intervals(settings)
    enabled = registry.list_enabled()
    if not enabled:
        return analysis_intervals(settings)
    intervals: set[str] = set()
    for meta in enabled:
        intervals.update(_intervals_for_strategy(meta))
    if not intervals:
        return analysis_intervals(settings)
    return tuple(sorted(intervals))


def ws_kline_intervals(
    settings: BotSettings,
    *,
    registry: StrategyRegistry | None = None,
    extra: tuple[str, ...] = (),
) -> tuple[str, ...]:
    """Union of WS kline streams from runtime + strategy metadata + explicit extras."""
    base = set(analysis_intervals(settings))
    base.update(strategy_intervals(settings, registry))
    base.update(str(x) for x in extra if str(x).strip())
    return tuple(sorted(base))


def _intervals_for_strategy(meta: StrategyMetadata) -> tuple[str, ...]:
    trigger_tf = str(meta.trigger_tf or "15m")
    trigger_intervals = tuple(str(x).strip() for x in meta.trigger_intervals if str(x).strip())
    required_tfs = tuple(str(x).strip() for x in meta.required_tfs if str(x).strip())
    timeframes = tuple(str(x).strip() for x in meta.timeframes if str(x).strip())
    return tuple(dict.fromkeys((trigger_tf, *trigger_intervals, *required_tfs, *timeframes)))
