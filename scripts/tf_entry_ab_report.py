#!/usr/bin/env python3
"""TF entry A/B report from signal_outcomes (answers.md Part 1 / phase 4 P2).

Groups closed outcomes by setup_id and entry_tf_used to compare expired%,
win-rate, and MFE/MAE without mixing TF variables.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

_WIN = frozenset({"tp1_hit", "tp2_hit", "tp3_hit", "breakeven_stop", "trailing_stop"})
_LOSS = frozenset({"stop_loss"})
_EXPIRED = frozenset({"expired_pending", "expired_active", "expired"})


def _safe_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        parsed = float(value)
    except TypeError, ValueError:
        return None
    if parsed != parsed:
        return None
    return parsed


def load_outcomes(db_path: Path, *, days: int) -> list[dict[str, Any]]:
    since = (datetime.now(UTC) - timedelta(days=days)).isoformat()
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT setup_id, entry_tf, timeframe, result, mfe, mae, pnl_r_multiple
            FROM signal_outcomes
            WHERE closed_at >= ?
            """,
            (since,),
        ).fetchall()
    finally:
        conn.close()
    return [dict(row) for row in rows]


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    buckets: dict[str, dict[str, int | float | list[float]]] = defaultdict(
        lambda: {
            "total": 0,
            "wins": 0,
            "losses": 0,
            "expired": 0,
            "mfe_samples": [],
            "mae_samples": [],
        }
    )
    for row in rows:
        setup_id = str(row.get("setup_id") or "unknown")
        entry_tf = str(row.get("entry_tf") or row.get("timeframe") or "unknown")
        key = f"{setup_id}@{entry_tf}"
        bucket = buckets[key]
        bucket["total"] = int(bucket["total"]) + 1
        result = str(row.get("result") or "")
        if result in _WIN:
            bucket["wins"] = int(bucket["wins"]) + 1
        elif result in _LOSS:
            bucket["losses"] = int(bucket["losses"]) + 1
        elif result in _EXPIRED:
            bucket["expired"] = int(bucket["expired"]) + 1
        mfe = _safe_float(row.get("mfe"))
        mae = _safe_float(row.get("mae"))
        if mfe is not None:
            cast_mfe: list[float] = bucket["mfe_samples"]  # type: ignore[assignment]
            cast_mfe.append(mfe)
        if mae is not None:
            cast_mae: list[float] = bucket["mae_samples"]  # type: ignore[assignment]
            cast_mae.append(mae)

    report_rows: list[dict[str, Any]] = []
    for key, bucket in sorted(buckets.items(), key=lambda item: (-int(item[1]["total"]), item[0])):
        total = int(bucket["total"])
        wins = int(bucket["wins"])
        losses = int(bucket["losses"])
        expired = int(bucket["expired"])
        mfe_list = list(bucket["mfe_samples"])  # type: ignore[arg-type]
        mae_list = list(bucket["mae_samples"])  # type: ignore[arg-type]
        setup_id, _, entry_tf = key.partition("@")
        trades = wins + losses
        report_rows.append(
            {
                "setup_id": setup_id,
                "entry_tf": entry_tf,
                "total": total,
                "wins": wins,
                "losses": losses,
                "expired": expired,
                "win_rate": round(wins / trades, 4) if trades else None,
                "expired_rate": round(expired / total, 4) if total else None,
                "avg_mfe": round(sum(mfe_list) / len(mfe_list), 4) if mfe_list else None,
                "avg_mae": round(sum(mae_list) / len(mae_list), 4) if mae_list else None,
            }
        )
    return {"rows": report_rows, "outcome_count": len(rows)}


def main() -> int:
    parser = argparse.ArgumentParser(description="entry_tf A/B report from signal_outcomes")
    parser.add_argument("--db", type=Path, default=Path("data/bot/signals.db"))
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    if not args.db.is_file():
        print(f"database not found: {args.db}", file=sys.stderr)
        return 1

    payload = {
        "generated_at": datetime.now(UTC).isoformat(),
        "window_days": args.days,
        **summarize(load_outcomes(args.db, days=max(1, args.days))),
    }
    if args.json:
        print(json.dumps(payload, indent=2))
        return 0

    print(f"entry_tf A/B | window={args.days}d | outcomes={payload['outcome_count']}")
    for row in payload["rows"][:25]:
        print(
            f"  {row['setup_id']:24s} {row['entry_tf']:4s}  "
            f"n={row['total']:3d}  exp={row['expired_rate']}  "
            f"wr={row['win_rate']}  mfe={row['avg_mfe']}"
        )
    if len(payload["rows"]) > 25:
        print(f"  ... +{len(payload['rows']) - 25} more rows (use --json)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
