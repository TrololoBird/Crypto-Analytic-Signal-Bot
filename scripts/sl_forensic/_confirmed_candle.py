"""Infer confirmed-bar (df[-2]) usage for archived forensic cases."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

# fix-sl-A strategies that received confirmed-bar / df[-2] treatment.
CONFIRMED_BAR_STRATEGIES = frozenset({"whale_walls", "spread_strategy", "btc_correlation"})

# Approximate commit date for fix-sl-A (560b030). Used when features JSON lacks the flag.
FIX_SL_A_COMMIT_DATE = datetime(2026, 6, 5, tzinfo=UTC)


def _parse_signal_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def infer_confirmed_candle(
    *,
    setup_id: str,
    signal_created_at: str | None,
    features_snapshot: dict[str, Any] | None = None,
    assess_closed_valid: bool | None = None,
) -> int | None:
    """
    Return 1 if signal likely used confirmed bar, 0 if not, None if unknown.

    Priority:
      1. Explicit flag in features / indicator snapshot
      2. Candle-assessment from historical replay
      3. Approximation: setup in CONFIRMED_BAR_STRATEGIES after fix-sl-A date
    """
    snap = features_snapshot or {}
    for key in ("confirmed_bar", "entry_candle_was_confirmed", "confirmed_candle"):
        raw = snap.get(key)
        if raw is True or raw == 1 or str(raw).lower() in {"1", "true", "yes"}:
            return 1
        if raw is False or raw == 0 or str(raw).lower() in {"0", "false", "no"}:
            return 0

    if assess_closed_valid is True:
        return 1
    if assess_closed_valid is False:
        return 0

    # Approximation - flagged in archive as inferred, not telemetry-native.
    if setup_id in CONFIRMED_BAR_STRATEGIES:
        created = _parse_signal_dt(signal_created_at)
        if created is not None and created >= FIX_SL_A_COMMIT_DATE:
            return 1

    return None
