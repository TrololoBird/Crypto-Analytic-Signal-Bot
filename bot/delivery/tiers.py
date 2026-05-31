"""WATCH vs ACTION tier classification (target spec)."""

from __future__ import annotations

from dataclasses import dataclass

from ..domain.config import BotSettings
from ..domain.delivery_policy import is_r_class_setup
from ..domain.schemas import Signal


@dataclass(frozen=True, slots=True)
class TierDecision:
    tier: str  # "watch" | "action"
    reason: str = ""


def classify_tier(signal: Signal, settings: BotSettings) -> TierDecision:
    if is_r_class_setup(signal.setup_id) and settings.delivery.r_class_watch_only:
        return TierDecision(tier="watch", reason="r_class_watch_only")

    delivery = settings.delivery
    action_min = float(delivery.action_min_score)
    watch_min = float(delivery.watch_min_score)
    score = float(signal.score or 0.0)
    if score >= action_min:
        return TierDecision(tier="action", reason="score_action")
    if score >= watch_min:
        return TierDecision(tier="watch", reason="score_watch")
    return TierDecision(tier="watch", reason="below_watch_min")
