"""Kline interval scheduling helpers."""

from __future__ import annotations

from bot.domain.config import BotSettings


def analysis_intervals(settings: BotSettings) -> tuple[str, ...]:
    """Intervals that trigger symbol analysis on kline close."""
    raw = getattr(settings.runtime, "analysis_kline_intervals", None) or ("15m",)
    return tuple(str(x) for x in raw)


def ws_kline_intervals(settings: BotSettings, *, extra: tuple[str, ...] = ()) -> tuple[str, ...]:
    """Union of WS kline streams to subscribe for shortlist symbols."""
    base = set(analysis_intervals(settings))
    base.update(extra)
    return tuple(sorted(base))
