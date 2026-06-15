#!/usr/bin/env python3
"""A/B report: htf_conflict vs TP/SL ratio (answers50 Q50).

Reads signal_outcomes from the bot SQLite DB and compares win-rate / SL-rate
for rows tagged with HTF reversal conflict vs aligned HTF context.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path

_WIN = frozenset({"tp1_hit", "tp2_hit", "tp3_hit", "breakeven_stop", "trailing_stop"})
_LOSS = frozenset({"stop_loss"})


def _parse_features(raw: object) -> dict:
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        parsed = json.loads(str(raw))
    except TypeError, ValueError, json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _htf_conflict_group(features: dict, *, direction: str) -> str:
    """Infer conflict arm when explicit tag missing (answers50 Q50 fallback)."""
    if features.get("htf_reversal_conflict_bear"):
        return "conflict"
    reasons = features.get("htf_reversal_conflict") or features.get("mtf_reason")
    if isinstance(reasons, str) and reasons.startswith("htf_reversal_conflict"):
        return "conflict"
    bias_4h = str(features.get("bias_4h") or "neutral").lower()
    dir_norm = str(direction or "").lower()
    if dir_norm == "long" and bias_4h == "downtrend":
        return "conflict"
    if dir_norm == "short" and bias_4h == "uptrend":
        return "conflict"
    return "aligned"


def load_outcomes(db_path: Path, *, days: int) -> list[dict]:
    since = (datetime.now(UTC) - timedelta(days=days)).isoformat()
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT result, direction, setup_id, features, closed_at, entry_tf
            FROM signal_outcomes
            WHERE closed_at >= ?
            ORDER BY closed_at DESC
            """,
            (since,),
        ).fetchall()
    finally:
        conn.close()
    return [dict(row) for row in rows]


def summarize(rows: list[dict]) -> dict:
    arms: dict[str, Counter[str]] = {"conflict": Counter(), "aligned": Counter()}
    for row in rows:
        result = str(row.get("result") or "")
        if result not in _WIN | _LOSS:
            continue
        feat = _parse_features(row.get("features"))
        arm = _htf_conflict_group(feat, direction=str(row.get("direction") or ""))
        arms[arm][result] += 1

    report: dict[str, dict] = {}
    for arm, counter in arms.items():
        wins = sum(counter[r] for r in _WIN)
        losses = sum(counter[r] for r in _LOSS)
        total = wins + losses
        report[arm] = {
            "wins": wins,
            "stop_losses": losses,
            "total": total,
            "win_rate": round(wins / total, 4) if total else None,
            "sl_rate": round(losses / total, 4) if total else None,
        }
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="htf_conflict A/B report from signal_outcomes")
    parser.add_argument(
        "--db",
        type=Path,
        default=Path("data/bot/signals.db"),
        help="SQLite path (default: data/bot/signals.db)",
    )
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--json", action="store_true", help="Emit JSON only")
    args = parser.parse_args()

    if not args.db.is_file():
        print(f"database not found: {args.db}", file=sys.stderr)
        return 1

    rows = load_outcomes(args.db, days=max(1, args.days))
    report = summarize(rows)
    payload = {
        "generated_at": datetime.now(UTC).isoformat(),
        "window_days": args.days,
        "outcome_rows": len(rows),
        "arms": report,
        "notes": "Run after 50+ activated outcomes post-Q45 before changing htf_conflict policy.",
    }
    if args.json:
        print(json.dumps(payload, indent=2))
        return 0

    print(f"htf_conflict A/B | window={args.days}d | rows={len(rows)}")
    for arm, stats in report.items():
        print(
            f"  {arm:8s}  n={stats['total']:3d}  "
            f"win_rate={stats['win_rate']}  sl_rate={stats['sl_rate']}"
        )
    conflict = report.get("conflict", {})
    aligned = report.get("aligned", {})
    if conflict.get("total", 0) >= 10 and aligned.get("total", 0) >= 10:
        if (conflict.get("sl_rate") or 0) > (aligned.get("sl_rate") or 0) * 1.5:
            print("  → conflict arm shows elevated SL rate (filter likely working)")
        else:
            print("  → conflict arm SL rate not 2× aligned — review MTF gate telemetry")
    else:
        print("  → insufficient sample per arm (need ~50+ each for decision)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
