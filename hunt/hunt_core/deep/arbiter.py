"""Module 1 Deep arbiter — pinned change + verdict queue cooldowns."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

_DEEP_COOLDOWN: dict[str, datetime] = {}
DEFAULT_STALE_HOURS = 4.0


def deep_cooldown_ok(symbol: str, *, now: datetime | None = None, hours: float = DEFAULT_STALE_HOURS) -> bool:
    now = now or datetime.now(UTC)
    last = _DEEP_COOLDOWN.get(symbol.upper())
    if last is None:
        return True
    return now - last >= timedelta(hours=hours)


def mark_deep_sent(symbol: str, *, now: datetime | None = None) -> None:
    _DEEP_COOLDOWN[symbol.upper()] = now or datetime.now(UTC)


def evaluate_deep_delivery(*, symbol: str, verdict: dict[str, Any]) -> tuple[bool, list[str]]:
    blockers: list[str] = []
    if not deep_cooldown_ok(symbol):
        blockers.append("deep_cooldown")
    decision = str(verdict.get("decision") or verdict.get("signal_decision") or "")
    if decision.upper() in {"WAIT", "NONE", ""}:
        blockers.append("decision_wait")
    return len(blockers) == 0, blockers


__all__ = [
    "DEFAULT_STALE_HOURS",
    "deep_cooldown_ok",
    "evaluate_deep_delivery",
    "mark_deep_sent",
]
