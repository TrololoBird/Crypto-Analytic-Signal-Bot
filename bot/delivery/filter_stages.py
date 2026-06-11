"""Composable filter pipeline stages (shortlist Phase 6)."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from engine.domain.config import BotSettings

DEFAULT_FILTER_STAGES: tuple[str, ...] = (
    "freshness",
    "entry_staleness",
    "mark_deviation",
    "spread",
    "atr",
    "stop",
    "rr",
    "scoring",
    "min_score",
)


def enabled_filter_stages(settings: BotSettings) -> frozenset[str]:
    configured = getattr(settings.filters, "enabled_stages", None)
    if not configured:
        return frozenset(DEFAULT_FILTER_STAGES)
    normalized = frozenset(str(item).strip().lower() for item in configured if str(item).strip())
    return normalized or frozenset(DEFAULT_FILTER_STAGES)


def filter_stage_enabled(settings: BotSettings, stage: str) -> bool:
    return str(stage).strip().lower() in enabled_filter_stages(settings)
