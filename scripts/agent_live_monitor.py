#!/usr/bin/env python3
"""Short live monitor: bot health, deliveries, tracking, recent errors."""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

try:
    from scripts.common import bootstrap_repo_path
except ModuleNotFoundError:
    from common import bootstrap_repo_path

bootstrap_repo_path()

from bot.domain.config import load_settings


def _analysis_dir(telemetry_dir: Path) -> Path:
    analysis = telemetry_dir / "analysis"
    return analysis if analysis.is_dir() else telemetry_dir


def _telemetry_tail(telemetry_dir: Path, name: str, *, limit: int = 8) -> list[dict]:
    path = _analysis_dir(telemetry_dir) / name
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8", errors="replace").strip().splitlines()
    rows: list[dict] = []
    for line in lines[-limit:]:
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def main() -> int:
    settings = load_settings("config.toml")
    db = Path(settings.db_path)
    now = datetime.now(UTC)
    since = (now - timedelta(hours=6)).isoformat()

    print(f"=== Live monitor @ {now.isoformat()} ===")
    print(f"provider={settings.notifiers.provider} db={db}")

    if db.exists():
        con = sqlite3.connect(db)
        con.row_factory = sqlite3.Row
        cur = con.cursor()
        cur.execute(
            "SELECT status, COUNT(*) c FROM active_signals GROUP BY status"
        )
        print("tracking_open:", {r["status"]: r["c"] for r in cur.fetchall()})
        cur.execute(
            """
            SELECT symbol, direction, setup_id, status, created_at, activated_at,
                   entry_zone_touched_at, close_reason
            FROM active_signals
            WHERE status IN ('pending','active')
            ORDER BY created_at DESC LIMIT 8
            """
        )
        open_rows = cur.fetchall()
        if open_rows:
            print("open_signals:")
            for r in open_rows:
                print(f"  {dict(r)}")
        cur.execute(
            """
            SELECT COUNT(*) FROM signal_outcomes
            WHERE created_at >= ?
            """,
            (since,),
        )
        outcomes_6h = cur.fetchone()[0]
        print(f"outcomes_6h={outcomes_6h}")
        con.close()
    else:
        print("db_missing")

    tel = Path(settings.telemetry_dir)
    delivered = _telemetry_tail(tel, "delivery.jsonl", limit=5)
    rejected = _telemetry_tail(tel, "rejected.jsonl", limit=5)
    tracking = _telemetry_tail(tel, "tracking_events.jsonl", limit=8)
    print(f"recent_delivered={len(delivered)}")
    for row in delivered:
        print(
            f"  delivered {row.get('symbol')} {row.get('setup_id')} "
            f"ref={row.get('tracking_ref')} msg={row.get('message_id')}"
        )
    print(f"recent_rejected_top:")
    counts: dict[str, int] = {}
    for row in rejected:
        reason = str(row.get("reason") or row.get("stage") or "?")
        counts[reason] = counts.get(reason, 0) + 1
    for reason, cnt in sorted(counts.items(), key=lambda x: -x[1])[:6]:
        print(f"  {reason}: {cnt}")
    print("recent_tracking:")
    for row in tracking:
        print(
            f"  {row.get('event_type')} {row.get('symbol')} "
            f"ref={row.get('tracking_ref')} status={row.get('status')}"
        )

    pid_file = Path(settings.pid_file)
    if pid_file.exists():
        print(f"pid_file={pid_file.read_text().strip()}")
    else:
        print("pid_file=missing")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
