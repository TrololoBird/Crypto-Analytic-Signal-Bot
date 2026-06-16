"""Shared tracker close_reason classification for stats and gates."""
from __future__ import annotations



from typing import Any

WIN_REASONS = frozenset({"tp1", "tp2", "fix_profit_tp1", "fix_profit_tp2", "trailing_stop_profit"})
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


def is_polluted(row: dict[str, Any]) -> bool:
    """Canonical 'not a genuine live signal' test, shared by every reporter.

    A row is polluted (excluded from live win-rate) when it lacks the fields a
    real tracker open always records: an open timestamp, a detector score, and a
    fuel reading. Legacy/partial archive rows miss these and must never inflate
    or deflate live WR. Keep this the single definition — analyze_signals,
    outcomes_report and stats_report all import it so their n/WR reconcile.
    """
    return (
        not row.get("opened_at")
        or row.get("score") is None
        or row.get("fuel") is None
    )


def genuine_closed(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Closed rows that are both genuine (not polluted) and carry a close_reason."""
    return [r for r in rows if not is_polluted(r) and r.get("close_reason")]


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


def outcome_archive_key(record: dict[str, Any]) -> tuple[str, str, str] | None:
    """Stable id for one tracker open → close leg (dedupe concurrent watch writers)."""
    opened = record.get("opened_at")
    if not opened:
        return None
    return (
        str(record.get("symbol") or "").upper(),
        str(record.get("direction") or "").lower(),
        str(opened),
    )


def _outcome_already_archived(path: Any, key: tuple[str, str, str]) -> bool:
    import json
    from pathlib import Path

    p = Path(path)
    if not p.exists():
        return False
    try:
        lines = p.read_text(encoding="utf-8").splitlines()
    except OSError:
        return False
    for line in reversed(lines[-800:]):
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if outcome_archive_key(rec) == key:
            return True
    return False


def append_outcome_record(path: Any, record: dict[str, Any]) -> None:
    """Single-writer outcome log append (§8E / P10)."""
    import json
    from pathlib import Path

    key = outcome_archive_key(record)
    if key is not None and _outcome_already_archived(path, key):
        return
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, default=str) + "\n")


def kpi_bucket(record: dict[str, Any]) -> str:
    """scenario×direction×phase key for stats rollup."""
    scenario = str(record.get("scenario") or record.get("setup_id") or "unknown")
    direction = str(record.get("direction") or "?")
    phase = entry_lifecycle_phase(record)
    return f"{scenario}:{direction}:{phase}"


__all__ = [
    "LOSS_REASONS",
    "WIN_REASONS",
    "append_outcome_record",
    "entry_lifecycle_phase",
    "genuine_closed",
    "is_polluted",
    "kpi_bucket",
    "outcome_archive_key",
    "outcome_kind",
]
