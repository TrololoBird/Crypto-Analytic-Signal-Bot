"""Unified outcome label store — merges backtest, gate_edge, and live grades."""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterator

from hunt_core.contracts import OutcomeRecord, outcome_from_row
from hunt_core.paths import (
    BACKTEST_OUTCOMES,
    BACKTEST_OUTCOMES_ENRICHED,
    GATE_EDGE_OUTCOMES,
    SIGNAL_HISTORY,
    UNIFIED_LABELS,
)


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def iter_sources() -> Iterator[tuple[str, Path]]:
    yield "backtest", BACKTEST_OUTCOMES
    yield "backtest_enriched", BACKTEST_OUTCOMES_ENRICHED
    yield "gate_edge", GATE_EDGE_OUTCOMES


def load_unified(*, rebuild: bool = False) -> list[OutcomeRecord]:
    """Load or rebuild unified label table."""
    if UNIFIED_LABELS.exists() and not rebuild:
        return [
            outcome_from_row(json.loads(line), source=row.get("source", "unified"))
            for line in UNIFIED_LABELS.read_text(encoding="utf-8").splitlines()
            if line.strip()
            for row in [json.loads(line)]
        ]
    return list(rebuild_unified())


def rebuild_unified() -> list[OutcomeRecord]:
    """Merge all outcome JSONL sources into unified_labels.jsonl."""
    seen: set[str] = set()
    out: list[OutcomeRecord] = []
    UNIFIED_LABELS.parent.mkdir(parents=True, exist_ok=True)
    with UNIFIED_LABELS.open("w", encoding="utf-8") as fh:
        for source, path in iter_sources():
            for row in _load_jsonl(path):
                rec = outcome_from_row(row, source=source)
                key = f"{rec['symbol']}|{rec['direction']}|{rec.get('opened_at')}|{source}"
                if key in seen:
                    continue
                seen.add(key)
                out.append(rec)
                fh.write(json.dumps(dict(rec), separators=(",", ":")) + "\n")
    return out


def slice_stats(
    rows: list[OutcomeRecord] | None = None,
) -> dict[str, Any]:
    """Aggregate SL/TP rates by direction and lifecycle_phase."""
    data = rows if rows is not None else load_unified()
    buckets: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for rec in data:
        outcome = rec.get("bt_outcome") or "timeout"
        direction = rec.get("direction") or "short"
        phase = rec.get("lifecycle_phase") or "unknown"
        key = f"{direction}|{phase}"
        buckets[key][outcome] += 1
        buckets[key]["n"] += 1
    summary: dict[str, Any] = {}
    for key, counts in buckets.items():
        n = counts["n"]
        sl = counts.get("sl_hit", 0)
        tp1p = counts.get("tp1_hit", 0) + counts.get("tp2_hit", 0)
        summary[key] = {
            "n": n,
            "sl_rate": round(sl / n, 3) if n else None,
            "tp1_plus_rate": round(tp1p / n, 3) if n else None,
        }
    return summary


def live_closed_for_grade() -> list[dict[str, Any]]:
    """Closed live signals suitable for hold-to-target join."""
    rows = _load_jsonl(SIGNAL_HISTORY)
    return [r for r in rows if r.get("closed_at") and r.get("opened_at")]
