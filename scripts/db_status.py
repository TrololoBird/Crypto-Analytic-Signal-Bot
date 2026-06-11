#!/usr/bin/env python3
"""Print DB migration version, signal counts, and key config flags."""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

try:
    import scripts.common  # noqa: F401
except ModuleNotFoundError:  # pragma: no cover
    import common  # noqa: F401

from bot.migrations import migrate_db
from bot.persistence.db_status import collect_db_status
from bot.persistence.repository.memory import MemoryRepository
from engine.domain.config import load_settings


async def _run(*, config: Path, apply_migrations: bool) -> int:
    settings = load_settings(config)
    if apply_migrations:
        repo = MemoryRepository(settings.db_path, settings.data_dir)
        await repo.initialize()
        try:
            applied = await migrate_db(repo._require_conn())
            print(f"db_migrations_applied={applied}")
        finally:
            await repo.close()

    summary = await collect_db_status(settings)
    print(f"db_migration_version={summary.migration_version}")
    for version, description, applied_at in summary.migrations:
        print(f"  v{version}\t{description}\t{applied_at}")

    if summary.signal_counts:
        for status, count in sorted(summary.signal_counts.items()):
            print(f"signals_{status}={count}")
    else:
        print("signals_total=0")

    if summary.outcome_counts:
        for result, count in sorted(summary.outcome_counts.items()):
            print(f"signal_outcomes_{result}={count}")
        print(f"signal_outcomes_total={summary.outcomes_total}")
    else:
        print("signal_outcomes_total=0")

    delivery = settings.delivery
    webhook = settings.notifiers.webhook
    print(
        "delivery_caps:",
        delivery.action_cap_per_session,
        delivery.zero_delivery_alert_cycles,
    )
    print(
        "ops_alerts:",
        webhook.ops_alerts_enabled,
        "url_set=",
        bool(str(webhook.webhook_url or "").strip()),
    )
    print("enforce_mtf:", delivery.enforce_mtf_gate)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("config.toml"))
    parser.add_argument(
        "--apply-migrations",
        action="store_true",
        help="Run forward migrations before reporting",
    )
    args = parser.parse_args()
    return asyncio.run(_run(config=args.config, apply_migrations=args.apply_migrations))


if __name__ == "__main__":
    sys.exit(main())
