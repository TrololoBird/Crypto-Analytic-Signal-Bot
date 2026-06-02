"""WATCH vs ACTION tier classification (target spec)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from ..domain.config import BotSettings
from ..domain.delivery_policy import effective_action_min_score, is_r_class_setup
from ..domain.schemas import Signal


@dataclass(frozen=True, slots=True)
class TierDecision:
    tier: str  # "watch" | "action"
    reason: str = ""


@dataclass(frozen=True, slots=True)
class TierCapDecision:
    symbol: str
    setup_id: str
    direction: str
    tier: Literal["watch", "action"]
    allow: bool
    reason: str
    drop_reason: str | None = None


def rank_key(signal: Signal) -> tuple[float, float]:
    """Match DeliveryOrchestrator ranking for deterministic cap ordering."""
    return (float(signal.score or 0.0), float(signal.risk_reward or 0.0))


def _resolve_caps(settings: BotSettings) -> tuple[int, int]:
    delivery = settings.delivery
    action_cap = int(delivery.action_cap_per_cycle)
    watch_cap = int(delivery.watch_cap_per_cycle)
    if action_cap > 0 and watch_cap > 0:
        return action_cap, watch_cap

    runtime_cap = int(getattr(settings.runtime, "max_signals_per_cycle", 0) or 0)
    if action_cap <= 0:
        action_cap = max(1, runtime_cap) if runtime_cap > 0 else 6
    if watch_cap <= 0:
        watch_cap = max(action_cap, runtime_cap) if runtime_cap > 0 else 12
    return action_cap, watch_cap


def classify_tier(signal: Signal, settings: BotSettings) -> TierDecision:
    if is_r_class_setup(signal.setup_id) and settings.delivery.r_class_watch_only:
        return TierDecision(tier="watch", reason="r_class_watch_only")

    delivery = settings.delivery
    action_min = effective_action_min_score(settings, signal.symbol)
    watch_min = float(delivery.watch_min_score)
    score = float(signal.score or 0.0)
    if score >= action_min:
        reason = "score_action"
        if action_min > float(delivery.action_min_score):
            reason = "score_action_anchor"
        return TierDecision(tier="action", reason=reason)
    if score >= watch_min:
        return TierDecision(tier="watch", reason="score_watch")
    return TierDecision(tier="watch", reason="below_watch_min")


def decide_with_caps(signals: list[Signal], settings: BotSettings) -> list[TierCapDecision]:
    """Classify tiers and enforce per-cycle WATCH/ACTION caps."""
    action_cap, watch_cap = _resolve_caps(settings)
    action_used = 0
    watch_used = 0
    decisions: list[TierCapDecision] = []
    ranked = sorted(signals, key=rank_key, reverse=True)
    for signal in ranked:
        tier = classify_tier(signal, settings)
        if tier.reason == "below_watch_min":
            decisions.append(
                TierCapDecision(
                    symbol=signal.symbol,
                    setup_id=signal.setup_id,
                    direction=signal.direction,
                    tier=tier.tier,
                    allow=False,
                    reason=tier.reason,
                    drop_reason="below_watch_min",
                )
            )
            continue

        allow = True
        drop_reason: str | None = None
        if tier.tier == "action":
            if action_used >= action_cap:
                allow = False
                drop_reason = "action_cap_reached"
            else:
                action_used += 1
        else:
            if watch_used >= watch_cap:
                allow = False
                drop_reason = "watch_cap_reached"
            else:
                watch_used += 1
        decisions.append(
            TierCapDecision(
                symbol=signal.symbol,
                setup_id=signal.setup_id,
                direction=signal.direction,
                tier=tier.tier,
                allow=allow,
                reason=tier.reason,
                drop_reason=drop_reason,
            )
        )
    return decisions
