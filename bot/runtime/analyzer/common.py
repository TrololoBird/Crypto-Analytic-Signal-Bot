from __future__ import annotations

import logging
from collections import Counter
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from bot.market.data import MarketDataUnavailable

if TYPE_CHECKING:
    from bot.domain.schemas import (
        Signal,
    )


LOG = logging.getLogger("bot.runtime.bot")
_DEGRADATION_ERRORS = (
    MarketDataUnavailable,
    RuntimeError,
    ValueError,
    TypeError,
    AttributeError,
    KeyError,
)
_DEFAULT_HISTORY_FETCH_LIMIT = 500
_HISTORY_FETCH_BUFFER_BARS = 60
_HISTORY_FETCH_BASELINE_BY_INTERVAL = {
    "5m": 300,
    "15m": 500,
    "1h": 500,
    "4h": 500,
}


def _history_fetch_limit(minimums: dict[str, int], interval: str) -> int:
    required = int(minimums.get(interval, 0))
    baseline = _HISTORY_FETCH_BASELINE_BY_INTERVAL.get(interval, 240)
    return max(baseline, required + _HISTORY_FETCH_BUFFER_BARS)


def _attach_rejection_rollups(
    funnel: dict[str, Any],
    rejected: list[dict[str, Any]],
) -> None:
    """Attach stage/setup reason rollups to the symbol funnel."""
    by_stage: Counter[str] = Counter()
    by_setup: Counter[str] = Counter()
    by_stage_reason: Counter[str] = Counter()
    by_setup_reason: Counter[str] = Counter()
    for row in rejected:
        stage = str(row.get("stage") or "unknown")
        setup_id = str(row.get("setup_id") or "unknown")
        reason = str(row.get("reason") or "unknown")
        by_stage[stage] += 1
        by_setup[setup_id] += 1
        by_stage_reason[f"{stage}:{reason}"] += 1
        by_setup_reason[f"{setup_id}:{reason}"] += 1
    funnel["rejects_by_stage"] = dict(by_stage)
    funnel["rejects_by_setup"] = dict(by_setup)
    funnel["reject_reasons_by_stage"] = dict(by_stage_reason)
    funnel["reject_reasons_by_setup"] = dict(by_setup_reason)


def _apply_setup_score_adjustment(
    signal: Signal, score_adjustment: float
) -> tuple[Signal, dict[str, Any]]:
    """Apply adaptive setup scoring without converting mild penalties into hard blocks."""
    try:
        adjustment = float(score_adjustment)
    except (TypeError, ValueError):
        adjustment = 0.0
    if not adjustment:
        return signal, {"applied": False, "adjustment": 0.0}

    adjusted_score = round(min(1.0, max(0.0, float(signal.score) + adjustment)), 4)
    if adjusted_score == signal.score:
        return signal, {"applied": False, "adjustment": adjustment}

    reason = "setup_performance_bonus" if adjustment > 0 else "setup_performance_penalty"
    reasons = signal.reasons if reason in signal.reasons else (*signal.reasons, reason)
    return (
        replace(signal, score=adjusted_score, reasons=reasons),
        {
            "applied": True,
            "adjustment": adjustment,
            "score_before": signal.score,
            "score_after": adjusted_score,
            "reason": reason,
        },
    )
