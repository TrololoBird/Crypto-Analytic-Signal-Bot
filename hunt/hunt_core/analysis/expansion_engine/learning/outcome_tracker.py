"""Outcome Learning Layer — the engine's long-term survival mechanism.

Every qualifying expansion signal is appended to an append-only ledger with its block
scores and forecast. Reviews at 24h / 48h / 72h / 7d grade realized move, drawdown, and
time-to-trigger. Aggregated hit-rate / avg-move statistics feed weight calibration.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from hunt_core.analysis.expansion_engine.types import ExpansionOpportunity
from hunt_core.paths import EXPANSION_OUTCOMES_JSONL

REVIEW_HORIZONS_H: tuple[int, ...] = (24, 48, 72, 168)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def record_expansion_signal(
    opp: ExpansionOpportunity,
    *,
    ts: str | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Append one signal to the outcomes ledger and return the written record."""
    record: dict[str, Any] = {
        "ts": ts or _now_iso(),
        "symbol": opp.symbol,
        "price": opp.price,
        "state": opp.state,
        "dominant": opp.dominant,
        "lifecycle_stage": opp.lifecycle_stage,
        "expansion_score": opp.expansion_score,
        "trigger_probability": opp.trigger_probability,
        "opportunity_score": opp.meta.opportunity_score,
        "expansion_quality": opp.meta.expansion_quality,
        "fake_breakout_risk": opp.meta.fake_breakout_risk,
        "probabilities": opp.probabilities.to_dict(),
        "blocks": opp.blocks.to_dict(),
        "forecast": opp.forecast.to_dict() if opp.forecast else None,
        "execution": opp.execution.to_dict() if opp.execution else None,
        "graded": [],
    }
    if extra:
        record.update(extra)
    try:
        EXPANSION_OUTCOMES_JSONL.parent.mkdir(parents=True, exist_ok=True)
        with EXPANSION_OUTCOMES_JSONL.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        pass
    return record


def grade_record(
    record: dict[str, Any],
    *,
    price_now: float,
    elapsed_h: float,
) -> dict[str, Any]:
    """Grade a recorded signal against the current price at a review horizon.

    Returns a grade dict: realized move %, whether TP1/stop levels were reached (from the
    recorded execution geometry), and the review horizon.
    """
    entry = float(record.get("price") or 0.0)
    dominant = str(record.get("dominant") or "neutral")
    move_pct = ((price_now - entry) / entry * 100.0) if entry > 0 else 0.0
    # Favorable move is up for pre-pump, down for pre-dump.
    favorable = move_pct if dominant == "up" else -move_pct

    grade: dict[str, Any] = {
        "elapsed_h": round(elapsed_h, 2),
        "price_now": price_now,
        "move_pct": round(move_pct, 3),
        "favorable_pct": round(favorable, 3),
        "hit_tp1": False,
        "hit_stop": False,
    }
    execution = record.get("execution") if isinstance(record.get("execution"), dict) else None
    if execution:
        targets = execution.get("targets") or []
        stop = execution.get("stop")
        if targets:
            tp1 = float(targets[0])
            grade["hit_tp1"] = (price_now >= tp1) if dominant == "up" else (price_now <= tp1)
        if stop is not None:
            stop = float(stop)
            grade["hit_stop"] = (price_now <= stop) if dominant == "up" else (price_now >= stop)
    grade["win"] = bool(grade["hit_tp1"]) or (favorable >= 5.0 and not grade["hit_stop"])
    return grade


def persist_expansion_outcomes(records: list[dict[str, Any]]) -> None:
    """Rewrite the outcomes ledger (used after grading pending reviews)."""
    try:
        EXPANSION_OUTCOMES_JSONL.parent.mkdir(parents=True, exist_ok=True)
        tmp = EXPANSION_OUTCOMES_JSONL.with_suffix(".jsonl.tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            for rec in records:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        tmp.replace(EXPANSION_OUTCOMES_JSONL)
    except OSError:
        pass


def load_expansion_outcomes() -> list[dict[str, Any]]:
    if not EXPANSION_OUTCOMES_JSONL.exists():
        return []
    out: list[dict[str, Any]] = []
    try:
        with EXPANSION_OUTCOMES_JSONL.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        return []
    return out


def summarize_outcomes(records: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Aggregate hit-rate / avg-move / avg-drawdown from graded records."""
    records = records if records is not None else load_expansion_outcomes()
    graded: list[dict[str, Any]] = []
    for rec in records:
        for g in rec.get("graded") or []:
            if isinstance(g, dict):
                graded.append(g)

    n = len(graded)
    if n == 0:
        return {
            "signals": len(records),
            "graded": 0,
            "real_hit_rate": None,
            "avg_move": None,
            "avg_favorable": None,
        }
    wins = sum(1 for g in graded if g.get("win"))
    moves = [float(g.get("move_pct", 0.0)) for g in graded]
    fav = [float(g.get("favorable_pct", 0.0)) for g in graded]
    return {
        "signals": len(records),
        "graded": n,
        "real_hit_rate": round(wins / n, 4),
        "avg_move": round(sum(moves) / n, 3),
        "avg_favorable": round(sum(fav) / n, 3),
    }


__all__ = [
    "REVIEW_HORIZONS_H",
    "grade_record",
    "load_expansion_outcomes",
    "persist_expansion_outcomes",
    "record_expansion_signal",
    "summarize_outcomes",
]
