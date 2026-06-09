#!/usr/bin/env python3
"""Report setup_id temporal overlap for divergence-cluster merge decisions (Phase 4)."""

from __future__ import annotations

import argparse
import asyncio
import json
from collections import defaultdict
from datetime import datetime
from itertools import combinations
from pathlib import Path

import aiosqlite


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


async def run_report(db_path: Path, *, window_minutes: int = 30) -> dict[str, object]:
    async with aiosqlite.connect(db_path) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(
            """
            SELECT setup_id, symbol, direction, created_at, entry_tf, result
            FROM signal_outcomes
            WHERE created_at IS NOT NULL
            ORDER BY created_at DESC
            LIMIT 5000
            """
        ) as cursor:
            rows = [dict(r) for r in await cursor.fetchall()]

    by_setup: dict[str, list[dict[str, object]]] = defaultdict(list)
    for row in rows:
        by_setup[str(row["setup_id"])].append(row)

    clusters = (
        ("indicator_divergence", "hidden_divergence", "rsi_divergence_bottom"),
        ("order_block", "breaker_block"),
        ("liquidity_sweep",),
    )
    overlap_report: list[dict[str, object]] = []
    for cluster in clusters:
        present = [sid for sid in cluster if sid in by_setup]
        for a, b in combinations(present, 2):
            overlaps = 0
            for ra in by_setup[a]:
                ta = _parse_dt(str(ra.get("created_at")))
                if ta is None:
                    continue
                for rb in by_setup[b]:
                    if ra.get("symbol") != rb.get("symbol") or ra.get("direction") != rb.get(
                        "direction"
                    ):
                        continue
                    tb = _parse_dt(str(rb.get("created_at")))
                    if tb is None:
                        continue
                    if abs((ta - tb).total_seconds()) <= window_minutes * 60:
                        overlaps += 1
                        break
            overlap_report.append(
                {
                    "pair": [a, b],
                    "overlap_count": overlaps,
                    "window_minutes": window_minutes,
                }
            )

    entry_tf_stats: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for row in rows:
        setup = str(row.get("setup_id") or "unknown")
        tf = str(row.get("entry_tf") or row.get("timeframe") or "unknown")
        entry_tf_stats[setup][tf] += 1

    return {
        "total_outcomes": len(rows),
        "overlap_pairs": overlap_report,
        "entry_tf_by_setup": {k: dict(v) for k, v in entry_tf_stats.items()},
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Setup correlation overlap report")
    parser.add_argument("--db", type=Path, default=Path("data/bot/bot.db"))
    parser.add_argument("--window-minutes", type=int, default=30)
    parser.add_argument("--json-out", type=Path, default=None)
    args = parser.parse_args()
    report = asyncio.run(run_report(args.db, window_minutes=args.window_minutes))
    text = json.dumps(report, indent=2)
    print(text)
    if args.json_out is not None:
        args.json_out.write_text(text, encoding="utf-8")


if __name__ == "__main__":
    main()
