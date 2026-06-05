"""DB status summary — migration version and active signal counts."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from bot.migrations import fetch_schema_version_rows
from bot.persistence.repository.memory import MemoryRepository

if TYPE_CHECKING:
    from collections.abc import Mapping

    import aiosqlite

    from bot.domain.config import BotSettings


DELIVERY_AUDIT_ROW_KEYS = frozenset(
    {"symbol", "setup_id", "delivery_status", "ts", "message_id", "source"}
)


def normalize_delivery_audit_row(row: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize delivery/selected telemetry rows for audit joins."""
    return {
        "symbol": str(row.get("symbol") or ""),
        "setup_id": str(row.get("setup_id") or ""),
        "delivery_status": str(row.get("delivery_status") or row.get("status") or "unknown"),
        "ts": str(row.get("ts") or ""),
        "message_id": row.get("message_id"),
        "source": str(row.get("source") or "delivery"),
    }


@dataclass(frozen=True)
class DbStatusSummary:
    migration_version: int = 0
    migrations: list[tuple[int, str, str]] = field(default_factory=list)
    signal_counts: dict[str, int] = field(default_factory=dict)
    outcome_counts: dict[str, int] = field(default_factory=dict)

    @property
    def signals_total(self) -> int:
        return sum(self.signal_counts.values())

    @property
    def outcomes_total(self) -> int:
        return sum(self.outcome_counts.values())


async def collect_db_status_from_conn(conn: aiosqlite.Connection) -> DbStatusSummary:
    rows = await fetch_schema_version_rows(conn)
    cur = await conn.execute(
        "SELECT status, COUNT(*) FROM active_signals GROUP BY status ORDER BY status"
    )
    signal_rows = await cur.fetchall()
    outcome_counts: dict[str, int] = {}
    table_cur = await conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='signal_outcomes'"
    )
    if await table_cur.fetchone():
        outcome_cur = await conn.execute(
            "SELECT result, COUNT(*) FROM signal_outcomes GROUP BY result ORDER BY result"
        )
        outcome_rows = await outcome_cur.fetchall()
        outcome_counts = {str(result): int(count) for result, count in outcome_rows}
    return DbStatusSummary(
        migration_version=int(rows[-1][0]) if rows else 0,
        migrations=list(rows),
        signal_counts={str(status): int(count) for status, count in signal_rows},
        outcome_counts=outcome_counts,
    )


async def collect_db_status(settings: BotSettings) -> DbStatusSummary:
    """Return migration version and per-status signal counts (read-only)."""
    repo = MemoryRepository(settings.db_path, settings.data_dir)
    await repo.initialize()
    try:
        return await collect_db_status_from_conn(repo._require_conn())
    finally:
        await repo.close()


def format_db_status_html(summary: DbStatusSummary) -> str:
    """Compact HTML block for Telegram operator /status."""
    lines = [
        "<b>DB</b>",
        f"Migration: <code>v{summary.migration_version}</code>",
    ]
    if summary.signal_counts:
        parts = [
            f"{status} <code>{count}</code>"
            for status, count in sorted(summary.signal_counts.items())
        ]
        lines.append("Signals: " + " · ".join(parts))
    else:
        lines.append("Signals: <code>0</code>")
    if summary.outcome_counts:
        parts = [
            f"{result} <code>{count}</code>"
            for result, count in sorted(summary.outcome_counts.items())
        ]
        lines.append("Outcomes: " + " · ".join(parts))
    else:
        lines.append("Outcomes: <code>0</code>")
    return "\n".join(lines)
