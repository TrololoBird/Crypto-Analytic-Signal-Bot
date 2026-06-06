"""SQLite schema migrations for runtime repository.

Lightweight forward-only migration registry.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

    import aiosqlite

LOG = logging.getLogger("bot.migrations")


MIGRATIONS: Sequence[tuple[int, str, str]] = (
    (
        1,
        "bootstrap_schema_version",
        """
        CREATE TABLE IF NOT EXISTS schema_version (
            version INTEGER PRIMARY KEY,
            applied_at TEXT DEFAULT (datetime('now')),
            description TEXT DEFAULT ''
        );
        """,
    ),
    (
        3,
        "trader_diary",
        """
        CREATE TABLE IF NOT EXISTS trader_diary (
            id TEXT PRIMARY KEY,
            linked_signal_id TEXT,
            decision TEXT NOT NULL DEFAULT 'took_signal'
                CHECK(decision IN ('took_signal','ignored','counter_traded')),
            entry_price REAL,
            entry_time TEXT,
            size_amount REAL,
            leverage REAL,
            risk_percent REAL,
            sl_price REAL,
            sl_source TEXT DEFAULT 'bot'
                CHECK(sl_source IN ('bot','modified','manual')),
            tp_prices TEXT,
            tp_hit_level INTEGER,
            exit_price REAL,
            exit_time TEXT,
            exit_reason TEXT
                CHECK(exit_reason IN ('tp1','tp2','tp3','sl','breakeven','manual_close',NULL)),
            pnl_percent REAL,
            pnl_usd REAL,
            mood TEXT,
            tags TEXT,
            notes TEXT,
            screenshot_path TEXT,
            bot_signal_snapshot TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            closed_at TEXT,
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_diary_decision ON trader_diary(decision);
        CREATE INDEX IF NOT EXISTS idx_diary_entry_time ON trader_diary(entry_time);
        CREATE INDEX IF NOT EXISTS idx_diary_signal ON trader_diary(linked_signal_id);
        CREATE INDEX IF NOT EXISTS idx_diary_closed ON trader_diary(closed_at);
        """,
    ),
    (
        4,
        "relabel_legacy_setup_invalidated",
        """
        UPDATE active_signals
        SET close_reason = 'legacy_setup_invalidated'
        WHERE close_reason = 'setup_invalidated';
        UPDATE signal_outcomes
        SET result = 'legacy_setup_invalidated'
        WHERE result = 'setup_invalidated';
        """,
    ),
    (
        5,
        "active_signals_status_symbol_index",
        """
        CREATE INDEX IF NOT EXISTS idx_active_signals_status_symbol
            ON active_signals(status, symbol);
        """,
    ),
    (
        6,
        "trader_diary_symbol_column",
        """
        ALTER TABLE trader_diary ADD COLUMN symbol TEXT;
        CREATE INDEX IF NOT EXISTS idx_diary_symbol ON trader_diary(symbol);
        """,
    ),
    (
        7,
        "sl_forensics_table",
        """
        CREATE TABLE IF NOT EXISTS sl_forensics (
            tracking_id TEXT PRIMARY KEY,
            symbol TEXT NOT NULL,
            setup_id TEXT NOT NULL,
            direction TEXT NOT NULL,
            timeframe TEXT NOT NULL,
            forensic_type TEXT NOT NULL,
            forensic_subtype TEXT,
            label TEXT,
            sl_root_cause_legacy TEXT,
            mfe REAL DEFAULT 0.0,
            mae REAL DEFAULT 0.0,
            post_sl_favorable_pct REAL DEFAULT 0.0,
            post_sl_tp1_reached INTEGER DEFAULT 0,
            closed_candle_valid INTEGER,
            entry_deviation_pct REAL DEFAULT 0.0,
            btc_correlation_at_sl REAL,
            active_minutes INTEGER DEFAULT 0,
            score REAL,
            atr_pct REAL,
            recommendations TEXT,
            metrics TEXT,
            card_markdown TEXT,
            analyzed_at TEXT NOT NULL DEFAULT (datetime('now')),
            signal_created_at TEXT,
            sl_closed_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_sl_forensics_setup ON sl_forensics(setup_id);
        CREATE INDEX IF NOT EXISTS idx_sl_forensics_type ON sl_forensics(forensic_type);
        CREATE INDEX IF NOT EXISTS idx_sl_forensics_analyzed ON sl_forensics(analyzed_at);
        """,
    ),
    (
        8,
        "sl_forensics_v2_full_schema",
        """
        DROP TABLE IF EXISTS sl_forensics;
        CREATE TABLE IF NOT EXISTS sl_forensics (
            forensic_id     TEXT PRIMARY KEY,
            signal_id       TEXT NOT NULL,
            tracking_id     TEXT NOT NULL,
            setup_id        TEXT NOT NULL,
            symbol          TEXT NOT NULL,
            direction       TEXT NOT NULL,
            timeframe       TEXT NOT NULL,

            signal_created_at   TEXT,
            entry_activated_at  TEXT,
            sl_hit_at           TEXT,
            time_to_entry_min   INTEGER,
            time_to_sl_min      INTEGER,

            entry_price     REAL,
            sl_price        REAL,
            tp1_price       REAL,
            tp2_price       REAL,
            sl_distance_pct REAL,
            rr_ratio        REAL,

            post_sl_candles_analyzed    INTEGER,
            post_sl_max_adverse_pct     REAL,
            post_sl_max_recovery_pct    REAL,
            post_sl_tp1_reached         INTEGER,
            post_sl_tp1_candles         INTEGER,
            post_sl_price_at_close      REAL,

            btc_move_in_sl_candle_pct   REAL,
            btc_direction_match         TEXT,
            btc_caused_sl               INTEGER,

            score                       REAL,
            atr_pct                     REAL,
            spread_bps                  REAL,
            funding_rate                REAL,
            entry_deviation_atr_mult    REAL,
            entry_candle_was_confirmed  INTEGER,

            market_regime               TEXT,
            btc_bias                    TEXT,
            direction_vs_bias           TEXT,

            sl_type     TEXT,
            sl_subtype  TEXT,
            sl_verdict  TEXT,

            candles_signal_tf   TEXT,
            candles_1h          TEXT,
            candles_4h          TEXT,
            candles_btc_signal  TEXT,

            strategy_recheck_valid  INTEGER,
            indicator_snapshot      TEXT,

            analyzed_at     TEXT DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_sl_forensics_setup
            ON sl_forensics(setup_id);
        CREATE INDEX IF NOT EXISTS idx_sl_forensics_type
            ON sl_forensics(sl_type);
        CREATE INDEX IF NOT EXISTS idx_sl_forensics_symbol
            ON sl_forensics(symbol);
        CREATE INDEX IF NOT EXISTS idx_sl_forensics_tracking
            ON sl_forensics(tracking_id);
        """,
    ),
    (
        9,
        "drop_legacy_signals_outcomes",
        """
        DROP TABLE IF EXISTS outcomes;
        DROP TABLE IF EXISTS signals;
        """,
    ),
    (
        10,
        "market_data_cache",
        """
        CREATE TABLE IF NOT EXISTS market_data_cache (
            cache_key TEXT PRIMARY KEY,
            payload_json TEXT NOT NULL,
            fetched_at TEXT NOT NULL DEFAULT (datetime('now')),
            ttl_seconds INTEGER NOT NULL DEFAULT 3600
        );
        CREATE INDEX IF NOT EXISTS idx_market_data_cache_fetched
            ON market_data_cache(fetched_at);
        """,
    ),
    (
        11,
        "hot_query_indexes",
        """
        CREATE INDEX IF NOT EXISTS idx_signal_outcomes_created_result
            ON signal_outcomes(created_at, result);
        CREATE INDEX IF NOT EXISTS idx_sl_forensics_sl_hit_at
            ON sl_forensics(sl_hit_at);
        """,
    ),
)


def _migration_statements(sql: str) -> list[str]:
    statements: list[str] = []
    for chunk in sql.split(";"):
        statement = chunk.strip()
        if statement:
            statements.append(statement)
    return statements

# Migrations that assume repository DDL already created these tables (see memory.initialize).
_MIGRATION_REQUIRES_TABLES: dict[int, frozenset[str]] = {
    4: frozenset({"active_signals", "signal_outcomes"}),
    5: frozenset({"active_signals"}),
}


async def _table_exists(conn: aiosqlite.Connection, table_name: str) -> bool:
    async with conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ? LIMIT 1",
        (table_name,),
    ) as cursor:
        row = await cursor.fetchone()
    return row is not None


async def _migration_prerequisites_met(
    conn: aiosqlite.Connection,
    version: int,
) -> bool:
    required = _MIGRATION_REQUIRES_TABLES.get(version)
    if not required:
        return True
    for table_name in required:
        if not await _table_exists(conn, table_name):
            return False
    return True


async def _assert_integrity(conn: aiosqlite.Connection) -> None:
    async with conn.execute("PRAGMA integrity_check") as cursor:
        row = await cursor.fetchone()
    status = str(row[0]) if row and row[0] is not None else "missing"
    if status != "ok":
        msg = f"DB integrity check failed after migration: {status}"
        raise RuntimeError(msg)


async def fetch_schema_version(conn: aiosqlite.Connection) -> int:
    """Return highest applied migration version (0 if registry empty)."""
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_version (
            version INTEGER PRIMARY KEY,
            applied_at TEXT DEFAULT (datetime('now')),
            description TEXT DEFAULT ''
        )
        """
    )
    async with conn.execute(
        "SELECT COALESCE(MAX(version), 0) AS version FROM schema_version"
    ) as cursor:
        row = await cursor.fetchone()
    return int(row[0]) if row and row[0] is not None else 0


async def fetch_schema_version_rows(
    conn: aiosqlite.Connection,
) -> list[tuple[int, str, str]]:
    """Return all rows from schema_version ordered by version."""
    async with conn.execute(
        "SELECT version, description, applied_at FROM schema_version ORDER BY version"
    ) as cursor:
        rows = await cursor.fetchall()
    return [(int(v), str(d or ""), str(a or "")) for v, d, a in rows]


async def migrate_db(conn: aiosqlite.Connection) -> int:
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_version (
            version INTEGER PRIMARY KEY,
            applied_at TEXT DEFAULT (datetime('now')),
            description TEXT DEFAULT ''
        )
        """
    )
    async with conn.execute(
        "SELECT COALESCE(MAX(version), 0) AS version FROM schema_version"
    ) as cursor:
        row = await cursor.fetchone()
    current = int(row[0]) if row and row[0] is not None else 0

    applied = 0
    for version, description, sql in MIGRATIONS:
        if version <= current:
            continue
        if not await _migration_prerequisites_met(conn, version):
            LOG.info(
                "db migration deferred | version=%d description=%s "
                "reason=missing_prerequisite_tables",
                version,
                description,
            )
            continue
        await conn.execute("BEGIN IMMEDIATE")
        try:
            for statement in _migration_statements(sql):
                await conn.execute(statement)
            await conn.execute(
                "INSERT OR REPLACE INTO schema_version (version, description) VALUES (?, ?)",
                (version, description),
            )
            await conn.commit()
        except Exception:
            await conn.rollback()
            raise
        await _assert_integrity(conn)
        applied += 1
        LOG.info("db migration applied | version=%d description=%s", version, description)
    return applied
