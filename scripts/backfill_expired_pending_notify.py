#!/usr/bin/env python3
"""Backfill channel follow-ups for pending signals that expired without notify."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

try:
    from scripts.common import bootstrap_repo_path
except ModuleNotFoundError:
    from common import bootstrap_repo_path

bootstrap_repo_path()

from bot.domain.config import load_settings
from bot.persistence.repository import MemoryRepository
from bot.persistence.tracking import SignalTracker
from bot.telemetry import TelemetryStore

LOG = logging.getLogger("scripts.backfill_expired_pending_notify")


async def _run(*, dry_run: bool, config: Path) -> int:
    settings = load_settings(config)
    repo = MemoryRepository(settings.db_path, settings.data_dir)
    await repo.initialize()
    tracker = SignalTracker(
        settings,
        market_data=None,
        telemetry=TelemetryStore(settings.telemetry_dir),
        memory_repo=repo,
    )
    rows = await repo.get_active_signals(status="pending")
    candidates = [
        row
        for row in rows
        if row.get("pending_expires_at")
        and not row.get("activated_at")
        and row.get("signal_message_id")
    ]
    LOG.info("pending expired candidates | count=%d dry_run=%s", len(candidates), dry_run)
    if dry_run:
        await repo.close()
        return 0
    events = await tracker.review_open_signals(dry_run=False)
    expired = [event for event in events if event.event_type == "expired"]
    LOG.info("review produced expired events | count=%d", len(expired))
    await repo.close()
    return len(expired)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("config.toml"))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")
    return asyncio.run(_run(dry_run=args.dry_run, config=args.config))


if __name__ == "__main__":
    sys.exit(main())
