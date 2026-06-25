"""Block-weight calibration from the outcome ledger.

Light, transparent calibration: for each block, compare its average score in winning vs
losing graded signals. Blocks that are reliably higher in winners get a weight multiplier
> 1; reliably higher in losers get < 1. When enough samples exist, multipliers are
persisted to ``data/expansion_calibration.json`` and applied on the next config load.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from hunt_core.expansion.config import (
    EXPANSION_CALIBRATION_JSON,
    invalidate_expansion_config_cache,
)

_MIN_SAMPLES = 20


def _win_loss_split(records: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    wins: list[dict[str, Any]] = []
    losses: list[dict[str, Any]] = []
    for rec in records:
        graded = rec.get("graded") or []
        if not graded:
            continue
        # Use the latest grade as the verdict.
        verdict = graded[-1]
        blocks = rec.get("blocks") if isinstance(rec.get("blocks"), dict) else {}
        if not blocks:
            continue
        (wins if verdict.get("win") else losses).append(blocks)
    return wins, losses


def calibrate_block_weights(records: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    if records is None:
        from hunt_core.expansion.learning.outcome_tracker import (
            load_expansion_outcomes,
        )

        records = load_expansion_outcomes()

    wins, losses = _win_loss_split(records)
    n = len(wins) + len(losses)
    if n < _MIN_SAMPLES:
        return {"status": "insufficient_samples", "samples": n, "multipliers": {}}

    names: set[str] = set()
    for b in wins + losses:
        names.update(b.keys())

    def avg(rows: list[dict[str, Any]], key: str) -> float:
        vals = [float(r.get(key, 0.0) or 0.0) for r in rows]
        return sum(vals) / len(vals) if vals else 0.0

    multipliers: dict[str, float] = {}
    for name in sorted(names):
        win_avg = avg(wins, name)
        loss_avg = avg(losses, name)
        spread = win_avg - loss_avg
        # Map spread in [-1,1] to a gentle multiplier in [0.75, 1.25].
        multipliers[name] = round(max(0.75, min(1.25, 1.0 + 0.25 * spread)), 3)

    return {
        "status": "ok",
        "samples": n,
        "wins": len(wins),
        "losses": len(losses),
        "multipliers": multipliers,
    }


def write_calibration_rollup(records: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Compute and persist block-weight multipliers from graded outcomes."""
    report = calibrate_block_weights(records)
    report["computed_at"] = datetime.now(UTC).isoformat()
    try:
        EXPANSION_CALIBRATION_JSON.parent.mkdir(parents=True, exist_ok=True)
        EXPANSION_CALIBRATION_JSON.write_text(
            json.dumps(report, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        invalidate_expansion_config_cache()
    except OSError:
        pass
    return report


def maybe_refresh_calibration(*, force: bool = False) -> dict[str, Any]:
    """Refresh calibration file when enough graded samples exist."""
    from hunt_core.expansion.learning.outcome_tracker import load_expansion_outcomes

    records = load_expansion_outcomes()
    report = calibrate_block_weights(records)
    if report.get("status") != "ok" and not force:
        return report
    if report.get("status") == "ok" or force:
        return write_calibration_rollup(records)
    return report


__all__ = ["calibrate_block_weights", "maybe_refresh_calibration", "write_calibration_rollup"]
