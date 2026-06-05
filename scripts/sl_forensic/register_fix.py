#!/usr/bin/env python3
"""Register a fix in the forensic archive fix_history table.

Usage:
    python scripts/sl_forensic/register_fix.py \\
        --fix-id fix-sl-A \\
        --name "Entry staleness filter + df[-2]" \\
        --commit 560b030 \\
        --targets whale_walls,spread_strategy,btc_correlation \\
        --hypothesis "Reduce FALSE_SIGNAL and ENTRY_CHASE"
"""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import UTC, datetime

import aiosqlite

try:
    import _bootstrap
except ModuleNotFoundError:  # pragma: no cover
    from scripts.sl_forensic import _bootstrap  # noqa: F401

from scripts.sl_forensic._archive_migrations import migrate_forensic_archive
from scripts.sl_forensic._paths import FORENSIC_ARCHIVE_PATH, ensure_forensics_dir


async def register_fix(
    *,
    fix_id: str,
    name: str,
    commit: str,
    targets: list[str],
    hypothesis: str,
    description: str = "",
    status: str = "ACTIVE",
) -> None:
    ensure_forensics_dir()
    applied_at = datetime.now(UTC).isoformat()
    async with aiosqlite.connect(FORENSIC_ARCHIVE_PATH) as conn:
        await migrate_forensic_archive(conn)
        await conn.execute(
            """
            INSERT INTO fix_history (
                fix_id, fix_name, commit_hash, applied_at,
                target_setup_ids, description, hypothesis, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(fix_id) DO UPDATE SET
                fix_name = excluded.fix_name,
                commit_hash = excluded.commit_hash,
                applied_at = excluded.applied_at,
                target_setup_ids = excluded.target_setup_ids,
                description = excluded.description,
                hypothesis = excluded.hypothesis,
                status = excluded.status
            """,
            (
                fix_id,
                name,
                commit,
                applied_at,
                json.dumps(targets),
                description or name,
                hypothesis,
                status,
            ),
        )
        await conn.commit()
    print(f"Registered fix: {fix_id} ({status})")
    print(f"  commit: {commit}")
    print(f"  targets: {', '.join(targets)}")
    print(f"  hypothesis: {hypothesis}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Register fix in forensic archive")
    parser.add_argument("--fix-id", required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--commit", default="")
    parser.add_argument("--targets", required=True, help="Comma-separated setup_ids")
    parser.add_argument("--hypothesis", default="")
    parser.add_argument("--description", default="")
    parser.add_argument("--status", default="ACTIVE")
    args = parser.parse_args()
    targets = [t.strip() for t in args.targets.split(",") if t.strip()]
    asyncio.run(
        register_fix(
            fix_id=args.fix_id,
            name=args.name,
            commit=args.commit,
            targets=targets,
            hypothesis=args.hypothesis,
            description=args.description,
            status=args.status,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
