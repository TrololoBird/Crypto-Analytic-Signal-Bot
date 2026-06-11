"""Shared tracker close_reason classification for stats and gates."""

from __future__ import annotations

from typing import Any

WIN_REASONS = frozenset({"tp1", "tp2", "fix_profit_tp1", "fix_profit_tp2"})
LOSS_REASONS = frozenset(
    {
        "stop_hit",
        "bounce_invalidate",
        "trend_exhaustion",
        "reclaim_invalidation",
        "support_lost",
        "bias_flip",
        "lifecycle_stale",
        "opposite_signal",
    }
)
LEGACY_UNKNOWN = "legacy_unknown"
# Profitable structural exits count as wins for WR (Q29 BE buffer).
_STRUCTURAL_EXIT_REASONS = frozenset(
    {
        "bounce_invalidate",
        "lifecycle_stale",
        "bias_flip",
        "trend_exhaustion",
    }
)
_PROFIT_STRUCTURAL_EXIT_MIN_PCT = 0.15


def entry_lifecycle_phase(sig: dict[str, Any]) -> str:
    """Immutable entry phase; fall back to lifecycle_phase for legacy rows."""
    return str(
        sig.get("entry_lifecycle_phase")
        or sig.get("lifecycle_phase")
        or sig.get("phase")
        or "?"
    )


def outcome_kind(reason: str, *, pnl_pct: float | None = None) -> str:
    if reason in WIN_REASONS:
        return "win"
    if reason in LOSS_REASONS:
        if (
            reason in _STRUCTURAL_EXIT_REASONS
            and pnl_pct is not None
            and float(pnl_pct) > _PROFIT_STRUCTURAL_EXIT_MIN_PCT
        ):
            return "win"
        return "loss"
    if reason == LEGACY_UNKNOWN and pnl_pct is not None:
        return "win" if float(pnl_pct) > 0 else "loss" if float(pnl_pct) < 0 else "flat"
    return "unknown"
