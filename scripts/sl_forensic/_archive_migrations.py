"""Additive migrations for forensic_archive.db (never reset)."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import aiosqlite

LOG = logging.getLogger("sl_forensic.archive_migrations")

MIGRATIONS: tuple[tuple[int, str], ...] = (
    (
        1,
        """
        CREATE TABLE IF NOT EXISTS schema_version_forensic (
            version INTEGER PRIMARY KEY,
            applied_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS forensic_cases (
            forensic_id         TEXT PRIMARY KEY,
            run_id              TEXT NOT NULL,
            run_date            TEXT NOT NULL,
            bot_version         TEXT,
            signal_id           TEXT,
            tracking_id         TEXT UNIQUE,
            setup_id            TEXT NOT NULL,
            symbol              TEXT NOT NULL,
            direction           TEXT NOT NULL,
            timeframe           TEXT NOT NULL,
            result              TEXT NOT NULL,
            pnl_pct             REAL,
            signal_created_at   TEXT,
            entry_activated_at  TEXT,
            sl_hit_at           TEXT,
            time_to_entry_min   INTEGER,
            time_to_sl_min      INTEGER,
            entry_price         REAL,
            sl_price            REAL,
            tp1_price           REAL,
            rr_ratio            REAL,
            sl_distance_pct     REAL,
            score               REAL,
            atr_pct             REAL,
            spread_bps          REAL,
            sl_type             TEXT,
            sl_subtype          TEXT,
            sl_verdict          TEXT,
            post_sl_tp1_reached     INTEGER,
            post_sl_tp1_candles     INTEGER,
            post_sl_max_recovery    REAL,
            post_sl_max_adverse     REAL,
            btc_move_sl_candle_pct  REAL,
            btc_direction_match     TEXT,
            btc_caused_sl           INTEGER,
            confirmed_candle        INTEGER,
            entry_deviation_atr     REAL,
            false_signal_recheck    INTEGER,
            market_regime           TEXT,
            btc_bias                TEXT,
            direction_vs_bias       TEXT,
            fixes_applied           TEXT,
            codebase_hash           TEXT,
            indicator_snapshot      TEXT,
            mfe                     REAL,
            mae                     REAL,
            analyzed_at             TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS forensic_runs (
            run_id          TEXT PRIMARY KEY,
            run_date        TEXT NOT NULL,
            codebase_hash   TEXT,
            total_signals   INTEGER,
            sl_count        INTEGER,
            tp_count        INTEGER,
            expired_count   INTEGER,
            fixes_active    TEXT,
            notes           TEXT,
            created_at      TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS fix_history (
            fix_id          TEXT PRIMARY KEY,
            fix_name        TEXT NOT NULL,
            commit_hash     TEXT,
            applied_at      TEXT,
            target_setup_ids TEXT,
            description     TEXT,
            hypothesis      TEXT,
            status          TEXT DEFAULT 'ACTIVE'
        );

        CREATE INDEX IF NOT EXISTS idx_fc_setup ON forensic_cases(setup_id);
        CREATE INDEX IF NOT EXISTS idx_fc_type ON forensic_cases(sl_type);
        CREATE INDEX IF NOT EXISTS idx_fc_run ON forensic_cases(run_id);
        CREATE INDEX IF NOT EXISTS idx_fc_false_signal ON forensic_cases(false_signal_recheck);
        CREATE INDEX IF NOT EXISTS idx_fc_tracking ON forensic_cases(tracking_id);
        CREATE INDEX IF NOT EXISTS idx_fc_result ON forensic_cases(result);
        """,
    ),
)


async def _current_version(conn: aiosqlite.Connection) -> int:
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_version_forensic (
            version INTEGER PRIMARY KEY,
            applied_at TEXT DEFAULT (datetime('now'))
        )
        """
    )
    async with conn.execute(
        "SELECT COALESCE(MAX(version), 0) FROM schema_version_forensic"
    ) as cursor:
        row = await cursor.fetchone()
    return int(row[0]) if row and row[0] is not None else 0


async def migrate_forensic_archive(conn: aiosqlite.Connection) -> int:
    """Run additive forensic archive migrations. Returns count applied."""
    current = await _current_version(conn)
    applied = 0
    for version, sql in MIGRATIONS:
        if version <= current:
            continue
        await conn.executescript(sql)
        await conn.execute(
            "INSERT OR REPLACE INTO schema_version_forensic (version) VALUES (?)",
            (version,),
        )
        await conn.commit()
        applied += 1
        LOG.info("forensic archive migration applied | version=%d", version)
    return applied
