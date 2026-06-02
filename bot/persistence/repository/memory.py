"""SQLite + parquet persistence for signals and outcomes."""

from __future__ import annotations

import json
import logging
import math
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import aiosqlite
import polars as pl

from ...migrations import migrate_db
from .schema import (
    SIGNAL_ANALYSIS_SCHEMA,
    OutcomeRecord,
    SignalRecord,
)

LOG = logging.getLogger("bot.persistence.repository")
_REPOSITORY_SCHEMA_VERSION = 2


class MemoryRepository:
    """Unified repository for signals and outcomes.

    Uses SQLite for metadata and Parquet for time-series data.
    Supports async operations and batch inserts.
    """

    def __init__(self, db_path: Path | str, data_dir: Path | str):
        self._db_path = Path(db_path)
        self._data_dir = Path(data_dir)
        self._data_dir.mkdir(parents=True, exist_ok=True)
        self._conn: aiosqlite.Connection | None = None
        self._extended_tables_ready = False

    def _require_conn(self) -> aiosqlite.Connection:
        conn = self._conn
        if conn is None:
            msg = "Repository not initialized"
            raise RuntimeError(msg)
        return conn

    async def _repository_schema_version(self) -> int:
        conn = self._require_conn()
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
        return int(row["version"] if row and row["version"] is not None else 0)

    async def _set_repository_schema_version(self, version: int, description: str) -> None:
        conn = self._require_conn()
        await conn.execute(
            "INSERT OR REPLACE INTO schema_version (version, description) VALUES (?, ?)",
            (int(version), description),
        )

    async def initialize(self) -> None:
        """Initialize database tables."""
        self._conn = await aiosqlite.connect(self._db_path, timeout=30.0)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA busy_timeout=30000")
        await self._conn.execute("PRAGMA journal_mode=WAL")
        schema_version = await self._repository_schema_version()

        # Create tables
        await self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS signals (
                signal_id TEXT PRIMARY KEY,
                symbol TEXT NOT NULL,
                strategy_id TEXT NOT NULL,
                direction TEXT NOT NULL,
                entry_price REAL NOT NULL,
                stop_loss REAL NOT NULL,
                take_profit_1 REAL NOT NULL,
                take_profit_2 REAL NOT NULL,
                score REAL NOT NULL,
                created_at TEXT NOT NULL,
                timeframe TEXT DEFAULT '1h',
                atr_pct REAL DEFAULT 0.0,
                spread_bps REAL DEFAULT 0.0,
                rsi_1h REAL,
                adx_1h REAL,
                volume_ratio REAL,
                funding_rate REAL,
                oi_change_pct REAL,
                features TEXT,  -- JSON
                metadata TEXT,  -- JSON
                outcome_id TEXT  -- Reference to outcome
            );

            CREATE INDEX IF NOT EXISTS idx_signals_symbol ON signals(symbol);
            CREATE INDEX IF NOT EXISTS idx_signals_strategy ON signals(strategy_id);
            CREATE INDEX IF NOT EXISTS idx_signals_created ON signals(created_at);

            CREATE TABLE IF NOT EXISTS outcomes (
                outcome_id TEXT PRIMARY KEY,
                signal_id TEXT NOT NULL,
                symbol TEXT NOT NULL,
                price_1h REAL,
                price_4h REAL,
                price_24h REAL,
                pnl_1h REAL,
                pnl_4h REAL,
                pnl_24h REAL,
                max_profit_pct REAL DEFAULT 0.0,
                max_loss_pct REAL DEFAULT 0.0,
                mae REAL DEFAULT 0.0,
                mfe REAL DEFAULT 0.0,
                hit_tp1 INTEGER DEFAULT 0,
                hit_tp2 INTEGER DEFAULT 0,
                hit_sl INTEGER DEFAULT 0,
                result TEXT DEFAULT '',
                updated_at TEXT NOT NULL,
                closed_at TEXT,
                time_to_tp1_min INTEGER,
                time_to_tp2_min INTEGER,
                time_to_sl_min INTEGER,
                FOREIGN KEY (signal_id) REFERENCES signals(signal_id)
            );

            CREATE INDEX IF NOT EXISTS idx_outcomes_symbol ON outcomes(symbol);
            CREATE INDEX IF NOT EXISTS idx_outcomes_signal ON outcomes(signal_id);
            CREATE INDEX IF NOT EXISTS idx_outcomes_result ON outcomes(result);

            CREATE TABLE IF NOT EXISTS config_versions (
                version_id TEXT PRIMARY KEY,
                config_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                is_active INTEGER DEFAULT 0
            );

            -- Cooldown tracking (replaces SignalCooldownStore JSON)
            CREATE TABLE IF NOT EXISTS cooldowns (
                cooldown_key TEXT PRIMARY KEY,
                last_sent_at TEXT NOT NULL,
                setup_id TEXT,
                symbol TEXT,
                cooldown_type TEXT DEFAULT 'signal_key'  -- 'signal_key' or 'symbol'
            );

            CREATE INDEX IF NOT EXISTS idx_cooldowns_symbol ON cooldowns(symbol);
            CREATE INDEX IF NOT EXISTS idx_cooldowns_setup ON cooldowns(setup_id);

            -- Setup adaptive scoring (replaces setup_score_adjustments JSON)
            CREATE TABLE IF NOT EXISTS setup_scores (
                setup_id TEXT PRIMARY KEY,
                score_adjustment REAL DEFAULT 0.0,
                outcome_window TEXT,  -- JSON array of last 20 outcomes
                updated_at TEXT NOT NULL
            );

            -- Active signal tracking (replaces SignalTrackingStore JSON)
            CREATE TABLE IF NOT EXISTS active_signals (
                tracking_id TEXT PRIMARY KEY,
                tracking_ref TEXT NOT NULL,
                signal_key TEXT NOT NULL,
                symbol TEXT NOT NULL,
                setup_id TEXT NOT NULL,
                direction TEXT NOT NULL,
                timeframe TEXT,
                created_at TEXT NOT NULL,
                pending_expires_at TEXT,
                active_expires_at TEXT,
                entry_low REAL,
                entry_high REAL,
                entry_mid REAL,
                initial_stop REAL,
                stop REAL,
                take_profit_1 REAL,
                take_profit_2 REAL,
                take_profit_3 REAL,
                valid_until TEXT,
                scale_weights TEXT,
                ttl_bars INTEGER,
                single_target_mode INTEGER DEFAULT 0,
                target_integrity_status TEXT DEFAULT 'unchecked',
                score REAL,
                risk_reward REAL,
                reasons TEXT,  -- JSON array
                signal_message_id INTEGER,
                bias_4h TEXT DEFAULT 'neutral',
                quote_volume REAL,
                spread_bps REAL,
                atr_pct REAL,
                orderflow_delta_ratio REAL,
                status TEXT DEFAULT 'pending',  -- pending, active, closed
                activated_at TEXT,
                activation_price REAL,
                tp1_hit_at TEXT,
                tp2_hit_at TEXT,
                stop_price REAL,
                tp1_price REAL,
                tp2_price REAL,
                last_checked_at TEXT,
                last_price REAL,
                closed_at TEXT,
                close_reason TEXT,
                close_price REAL
            );

            CREATE INDEX IF NOT EXISTS idx_active_signals_symbol ON active_signals(symbol);
            CREATE INDEX IF NOT EXISTS idx_active_signals_status ON active_signals(status);
            CREATE INDEX IF NOT EXISTS idx_active_signals_setup ON active_signals(setup_id);
            CREATE INDEX IF NOT EXISTS idx_active_signals_created ON active_signals(created_at);

            CREATE TABLE IF NOT EXISTS tracking_stats (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                signals_sent INTEGER DEFAULT 0,
                activated INTEGER DEFAULT 0,
                tp1_hit INTEGER DEFAULT 0,
                tp2_hit INTEGER DEFAULT 0,
                stop_loss INTEGER DEFAULT 0,
                expired INTEGER DEFAULT 0,
                ambiguous_exit INTEGER DEFAULT 0
            );

            INSERT OR IGNORE INTO tracking_stats (id) VALUES (1);

            CREATE TABLE IF NOT EXISTS signal_outcomes (
                tracking_id TEXT PRIMARY KEY,
                signal_id TEXT NOT NULL,
                tracking_ref TEXT NOT NULL,
                symbol TEXT NOT NULL,
                setup_id TEXT NOT NULL,
                direction TEXT NOT NULL,
                timeframe TEXT NOT NULL,
                created_at TEXT NOT NULL,
                activated_at TEXT,
                closed_at TEXT,
                entry_price REAL,
                exit_price REAL,
                result TEXT NOT NULL,
                pnl_pct REAL DEFAULT 0.0,
                pnl_r_multiple REAL DEFAULT 0.0,
                max_profit_pct REAL DEFAULT 0.0,
                max_loss_pct REAL DEFAULT 0.0,
                mae REAL DEFAULT 0.0,
                mfe REAL DEFAULT 0.0,
                time_to_entry_min INTEGER DEFAULT 0,
                time_to_exit_min INTEGER DEFAULT 0,
                features TEXT,
                was_profitable INTEGER DEFAULT 0,
                llm_was_correct INTEGER,
                setup_quality TEXT DEFAULT 'neutral'
            );

            CREATE INDEX IF NOT EXISTS idx_signal_outcomes_symbol ON signal_outcomes(symbol);
            CREATE INDEX IF NOT EXISTS idx_signal_outcomes_setup ON signal_outcomes(setup_id);
            CREATE INDEX IF NOT EXISTS idx_signal_outcomes_result ON signal_outcomes(result);
            CREATE INDEX IF NOT EXISTS idx_signal_outcomes_closed_at ON signal_outcomes(closed_at);
        """)

        if schema_version < _REPOSITORY_SCHEMA_VERSION:
            await self._ensure_table_columns(
                "active_signals",
                {
                    "tp1_hit_at": "TEXT",
                    "tp2_hit_at": "TEXT",
                    "stop_price": "REAL",
                    "tp1_price": "REAL",
                    "tp2_price": "REAL",
                    "take_profit_3": "REAL",
                    "tp3_price": "REAL",
                    "valid_until": "TEXT",
                    "scale_weights": "TEXT",
                    "ttl_bars": "INTEGER",
                    "last_checked_at": "TEXT",
                    "last_price": "REAL",
                    "single_target_mode": "INTEGER DEFAULT 0",
                    "target_integrity_status": "TEXT DEFAULT 'unchecked'",
                },
            )
            await self._ensure_extended_tables(run_column_migrations=True)
            await self._set_repository_schema_version(
                _REPOSITORY_SCHEMA_VERSION,
                "repository_schema_consolidated",
            )
        else:
            await self._ensure_extended_tables()
        await migrate_db(self._conn)
        await self._conn.commit()
        LOG.info("Memory repository initialized at %s", self._db_path)

    async def _ensure_table_columns(self, table_name: str, columns: dict[str, str]) -> None:
        """Add missing columns for existing databases."""
        if not self._conn:
            msg = "Repository not initialized"
            raise RuntimeError(msg)

        async with self._conn.execute(f"PRAGMA table_info({table_name})") as cursor:
            existing_rows = await cursor.fetchall()
        existing = {row["name"] for row in existing_rows}

        for column_name, column_type in columns.items():
            if column_name in existing:
                continue
            await self._conn.execute(
                f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_type}"
            )

    async def _ensure_extended_tables(self, *, run_column_migrations: bool = False) -> None:
        """Create additional tables for market context and stats."""
        if self._extended_tables_ready and not run_column_migrations:
            return
        conn = self._require_conn()

        # Market context table
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS market_context (
                id INTEGER PRIMARY KEY,
                btc_bias TEXT DEFAULT 'neutral',
                eth_bias TEXT DEFAULT 'neutral',
                altcoin_season_index REAL DEFAULT 50.0,
                btc_phase TEXT DEFAULT 'sideways',
                high_funding_symbols TEXT DEFAULT '[]',
                low_funding_symbols TEXT DEFAULT '[]',
                updated_at TEXT,
                market_regime TEXT DEFAULT 'unknown',
                market_regime_confirmed INTEGER DEFAULT 0,
                macro_risk_mode TEXT DEFAULT 'normal',
                benchmark_context_json TEXT DEFAULT '{}',
                intelligence_json TEXT DEFAULT '{}'
            )
        """)
        if run_column_migrations:
            async with conn.execute("PRAGMA table_info(market_context)") as cursor:
                existing_columns = {str(row["name"]) for row in await cursor.fetchall()}
            for column_name, column_sql in (
                (
                    "altcoin_season_index",
                    "ALTER TABLE market_context ADD COLUMN altcoin_season_index REAL DEFAULT 50.0",
                ),
                (
                    "btc_phase",
                    "ALTER TABLE market_context ADD COLUMN btc_phase TEXT DEFAULT 'sideways'",
                ),
                (
                    "market_regime",
                    "ALTER TABLE market_context ADD COLUMN market_regime TEXT DEFAULT 'unknown'",
                ),
                (
                    "market_regime_confirmed",
                    (
                        "ALTER TABLE market_context ADD COLUMN market_regime_confirmed "
                        "INTEGER DEFAULT 0"
                    ),
                ),
                (
                    "macro_risk_mode",
                    "ALTER TABLE market_context ADD COLUMN macro_risk_mode TEXT DEFAULT 'normal'",
                ),
                (
                    "benchmark_context_json",
                    (
                        "ALTER TABLE market_context ADD COLUMN benchmark_context_json "
                        "TEXT DEFAULT '{}'"
                    ),
                ),
                (
                    "intelligence_json",
                    "ALTER TABLE market_context ADD COLUMN intelligence_json TEXT DEFAULT '{}'",
                ),
            ):
                if column_name not in existing_columns:
                    await conn.execute(column_sql)

        # Symbol stats table (for blacklist)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS symbol_stats (
                symbol TEXT PRIMARY KEY,
                total_signals INTEGER DEFAULT 0,
                tp1_hits INTEGER DEFAULT 0,
                tp2_hits INTEGER DEFAULT 0,
                sl_hits INTEGER DEFAULT 0,
                consecutive_sl INTEGER DEFAULT 0,
                last_signal_ts TEXT
            )
        """)

        # Setup stats table (for score multiplier)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS setup_stats (
                setup_key TEXT PRIMARY KEY,
                setup_id TEXT,
                direction TEXT,
                regime TEXT,
                wins INTEGER DEFAULT 0,
                losses INTEGER DEFAULT 0
            )
        """)

        await conn.commit()
        self._extended_tables_ready = True

    async def update_market_context(
        self,
        btc_bias: str,
        eth_bias: str,
        high_funding_symbols: list[str],
        low_funding_symbols: list[str],
        *,
        market_regime: str = "unknown",
        market_regime_confirmed: bool = False,
        macro_risk_mode: str = "normal",
        altcoin_season_index: float | None = None,
        btc_phase: str | None = None,
        benchmark_context: dict[str, Any] | None = None,
        intelligence_snapshot: dict[str, Any] | None = None,
    ) -> None:
        """Update market context in SQLite."""
        conn = self._require_conn()
        await self._ensure_extended_tables()

        await conn.execute(
            """
            INSERT OR REPLACE INTO market_context
            (
                id,
                btc_bias,
                eth_bias,
                altcoin_season_index,
                btc_phase,
                high_funding_symbols,
                low_funding_symbols,
                updated_at,
                market_regime,
                market_regime_confirmed,
                macro_risk_mode,
                benchmark_context_json,
                intelligence_json
            )
            VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
            (
                btc_bias,
                eth_bias,
                50.0 if altcoin_season_index is None else float(altcoin_season_index),
                str(btc_phase or "sideways"),
                json.dumps(high_funding_symbols),
                json.dumps(low_funding_symbols),
                datetime.now(UTC).isoformat(),
                market_regime,
                1 if market_regime_confirmed else 0,
                macro_risk_mode,
                json.dumps(benchmark_context or {}, ensure_ascii=True),
                json.dumps(intelligence_snapshot or {}, ensure_ascii=True),
            ),
        )
        await conn.commit()

    async def get_market_context(self) -> dict[str, Any]:
        """Get current market context."""
        conn = self._require_conn()
        await self._ensure_extended_tables()

        async with conn.execute("SELECT * FROM market_context WHERE id = 1") as cursor:
            row = await cursor.fetchone()
            if row:
                intelligence_snapshot: dict[str, Any] = {}
                raw_intelligence = row.get("intelligence_json", None)
                benchmark_context: dict[str, Any] = {}
                raw_benchmarks = row.get("benchmark_context_json", None)
                if raw_benchmarks:
                    try:
                        parsed_benchmarks = json.loads(raw_benchmarks)
                    except json.JSONDecodeError:
                        parsed_benchmarks = {}
                    if isinstance(parsed_benchmarks, dict):
                        benchmark_context = parsed_benchmarks
                if raw_intelligence:
                    try:
                        intelligence_snapshot = json.loads(raw_intelligence)
                    except json.JSONDecodeError:
                        intelligence_snapshot = {}
                return {
                    "btc_bias": row["btc_bias"],
                    "eth_bias": row["eth_bias"],
                    "sol_bias": str(
                        (benchmark_context.get("SOLUSDT") or {}).get("bias") or "neutral"
                    ),
                    "xau_bias": str(
                        (benchmark_context.get("XAUUSDT") or {}).get("bias") or "neutral"
                    ),
                    "xag_bias": str(
                        (benchmark_context.get("XAGUSDT") or {}).get("bias") or "neutral"
                    ),
                    "pax_bias": str(
                        (benchmark_context.get("PAXGUSDT") or {}).get("bias") or "neutral"
                    ),
                    "altcoin_season_index": float(row["altcoin_season_index"])
                    if "altcoin_season_index" in row and row["altcoin_season_index"] is not None
                    else 50.0,
                    "btc_phase": row.get("btc_phase", "sideways"),
                    "high_funding_symbols": json.loads(row["high_funding_symbols"]),
                    "low_funding_symbols": json.loads(row["low_funding_symbols"]),
                    "updated_at": row["updated_at"],
                    "market_regime": row.get("market_regime", "unknown"),
                    "market_regime_confirmed": bool(row["market_regime_confirmed"])
                    if "market_regime_confirmed" in row
                    else False,
                    "macro_risk_mode": row.get("macro_risk_mode", "normal"),
                    "benchmark_context": benchmark_context,
                    "intelligence_snapshot": intelligence_snapshot,
                }
            return {
                "btc_bias": "neutral",
                "eth_bias": "neutral",
                "sol_bias": "neutral",
                "xau_bias": "neutral",
                "xag_bias": "neutral",
                "pax_bias": "neutral",
                "altcoin_season_index": 50.0,
                "btc_phase": "sideways",
                "high_funding_symbols": [],
                "low_funding_symbols": [],
                "market_regime": "unknown",
                "market_regime_confirmed": False,
                "macro_risk_mode": "normal",
                "benchmark_context": {},
                "intelligence_snapshot": {},
            }

    async def record_symbol_outcome(
        self,
        symbol: str,
        setup_id: str,
        direction: str,
        regime: str,
        outcome: str,
    ) -> None:
        """Record outcome for symbol/setup stats."""
        conn = self._require_conn()
        await self._ensure_extended_tables()

        # Update symbol stats
        await conn.execute(
            """
            INSERT INTO symbol_stats (
                symbol, total_signals, tp1_hits, tp2_hits, sl_hits, consecutive_sl, last_signal_ts
            )
            VALUES (?, 1, ?, ?, ?, ?, ?)
            ON CONFLICT(symbol) DO UPDATE SET
                total_signals = total_signals + 1,
                tp1_hits = tp1_hits + ?,
                tp2_hits = tp2_hits + ?,
                sl_hits = sl_hits + ?,
                consecutive_sl = CASE WHEN ? = 'loss' THEN consecutive_sl + 1 ELSE 0 END,
                last_signal_ts = ?
        """,
            (
                symbol,
                1 if outcome in ("tp1", "tp2") else 0,
                1 if outcome == "tp2" else 0,
                1 if outcome == "loss" else 0,
                1 if outcome == "loss" else 0,
                datetime.now(UTC).isoformat(),
                1 if outcome in ("tp1", "tp2") else 0,
                1 if outcome == "tp2" else 0,
                1 if outcome == "loss" else 0,
                outcome,
                datetime.now(UTC).isoformat(),
            ),
        )

        # Update setup stats
        key = f"{setup_id}|{direction}|{regime}"
        is_win = outcome in ("tp1", "tp2")
        is_loss = outcome == "loss"

        await conn.execute(
            """
            INSERT INTO setup_stats (setup_key, setup_id, direction, regime, wins, losses)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(setup_key) DO UPDATE SET
                wins = wins + ?,
                losses = losses + ?
        """,
            (
                key,
                setup_id,
                direction,
                regime,
                int(is_win),
                int(is_loss),
                int(is_win),
                int(is_loss),
            ),
        )

        await conn.commit()

    async def is_symbol_blacklisted(
        self,
        symbol: str,
        max_sl_streak: int = 3,
        pause_hours: int = 0,
    ) -> bool:
        """Check if symbol is temporarily paused due to consecutive SL hits."""
        conn = self._require_conn()
        await self._ensure_extended_tables()

        async with conn.execute(
            "SELECT consecutive_sl, last_signal_ts FROM symbol_stats WHERE symbol = ?",
            (symbol,),
        ) as cursor:
            row = await cursor.fetchone()
            if not row or row["consecutive_sl"] < max_sl_streak:
                return False
            if pause_hours <= 0:
                return True
            try:
                last_signal_ts = datetime.fromisoformat(str(row["last_signal_ts"]))
            except (TypeError, ValueError):
                return True
            if last_signal_ts.tzinfo is None:
                last_signal_ts = last_signal_ts.replace(tzinfo=UTC)
            return (datetime.now(UTC) - last_signal_ts) < timedelta(hours=pause_hours)

    async def get_consecutive_sl(self, symbol: str) -> int:
        """Get consecutive SL streak for symbol."""
        conn = self._require_conn()
        await self._ensure_extended_tables()

        async with conn.execute(
            "SELECT consecutive_sl FROM symbol_stats WHERE symbol = ?", (symbol,)
        ) as cursor:
            row = await cursor.fetchone()
            return row["consecutive_sl"] if row else 0

    async def get_setup_score_multiplier(
        self,
        setup_id: str,
        direction: str,
        regime: str,
        min_samples: int = 10,
    ) -> float:
        """Get score multiplier based on setup win rate."""
        conn = self._require_conn()
        await self._ensure_extended_tables()

        key = f"{setup_id}|{direction}|{regime}"
        async with conn.execute(
            "SELECT wins, losses FROM setup_stats WHERE setup_key = ?", (key,)
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                wins = row["wins"]
                losses = row["losses"]
                total = wins + losses
                if total >= min_samples:
                    win_rate = wins / total
                    if win_rate >= 0.65:
                        return 1.10
                    if win_rate <= 0.40:
                        return 0.90
            return 1.0

    async def summary(self) -> dict[str, Any]:
        """Get summary for logging and Telegram."""
        conn = self._require_conn()
        await self._ensure_extended_tables()

        # Get blacklisted symbols
        async with conn.execute(
            "SELECT symbol FROM symbol_stats WHERE consecutive_sl >= 3"
        ) as cursor:
            blacklisted = [row["symbol"] for row in await cursor.fetchall()]

        # Get top setups by win rate
        async with conn.execute(
            "SELECT setup_id, wins, losses FROM setup_stats ORDER BY (wins + losses) DESC LIMIT 5"
        ) as cursor:
            top_setups = []
            for row in await cursor.fetchall():
                total = row["wins"] + row["losses"]
                win_rate = row["wins"] / total if total > 0 else 0
                top_setups.append(
                    {
                        "setup_id": row["setup_id"],
                        "win_rate": win_rate,
                        "samples": total,
                    }
                )

        return {
            "blacklisted_symbols": blacklisted,
            "top_setups": top_setups,
            "symbol_count": len(blacklisted),
        }

    async def close(self) -> None:
        """Close database connection."""
        if self._conn:
            await self._conn.close()
            self._conn = None

    async def save_signal(self, record: SignalRecord) -> None:
        """Save signal record."""
        if not self._conn:
            msg = "Repository not initialized"
            raise RuntimeError(msg)

        record.validate()
        try:
            await self._conn.execute(
                """
                INSERT OR REPLACE INTO signals (
                    signal_id, symbol, strategy_id, direction, entry_price,
                    stop_loss, take_profit_1, take_profit_2, score, created_at,
                    timeframe, atr_pct, spread_bps, rsi_1h, adx_1h, volume_ratio,
                    funding_rate, oi_change_pct, features, metadata
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
                (
                    record.signal_id,
                    record.symbol,
                    record.strategy_id,
                    record.direction,
                    record.entry_price,
                    record.stop_loss,
                    record.take_profit_1,
                    record.take_profit_2,
                    record.score,
                    record.created_at.isoformat(),
                    record.timeframe,
                    record.atr_pct,
                    record.spread_bps,
                    record.rsi_1h,
                    record.adx_1h,
                    record.volume_ratio,
                    record.funding_rate,
                    record.oi_change_pct,
                    json.dumps(record.features) if record.features else None,
                    json.dumps(record.metadata) if record.metadata else None,
                ),
            )
            await self._conn.commit()
        except Exception:
            LOG.exception("failed to save signal %s", record.signal_id)
            raise

    async def save_outcome(self, record: OutcomeRecord, *, commit: bool = True) -> None:
        """Save outcome record."""
        if not self._conn:
            msg = "Repository not initialized"
            raise RuntimeError(msg)

        record.validate()
        try:
            await self._conn.execute(
                """
                INSERT INTO outcomes (
                    outcome_id, signal_id, symbol, price_1h, price_4h, price_24h,
                    pnl_1h, pnl_4h, pnl_24h, max_profit_pct, max_loss_pct, mae, mfe,
                    hit_tp1, hit_tp2, hit_sl, result, updated_at, closed_at,
                    time_to_tp1_min, time_to_tp2_min, time_to_sl_min
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(outcome_id) DO UPDATE SET
                    price_1h = excluded.price_1h,
                    price_4h = excluded.price_4h,
                    price_24h = excluded.price_24h,
                    pnl_1h = excluded.pnl_1h,
                    pnl_4h = excluded.pnl_4h,
                    pnl_24h = excluded.pnl_24h,
                    max_profit_pct = excluded.max_profit_pct,
                    max_loss_pct = excluded.max_loss_pct,
                    mae = excluded.mae,
                    mfe = excluded.mfe,
                    hit_tp1 = excluded.hit_tp1,
                    hit_tp2 = excluded.hit_tp2,
                    hit_sl = excluded.hit_sl,
                    result = excluded.result,
                    updated_at = excluded.updated_at,
                    closed_at = excluded.closed_at
            """,
                (
                    record.outcome_id,
                    record.signal_id,
                    record.symbol,
                    record.price_1h,
                    record.price_4h,
                    record.price_24h,
                    record.pnl_1h,
                    record.pnl_4h,
                    record.pnl_24h,
                    record.max_profit_pct,
                    record.max_loss_pct,
                    record.mae,
                    record.mfe,
                    int(record.hit_tp1),
                    int(record.hit_tp2),
                    int(record.hit_sl),
                    record.result,
                    record.updated_at.isoformat(),
                    record.closed_at.isoformat() if record.closed_at else None,
                    record.time_to_tp1_min,
                    record.time_to_tp2_min,
                    record.time_to_sl_min,
                ),
            )
            if commit:
                await self._conn.commit()
        except Exception:
            LOG.exception("failed to save outcome %s", record.outcome_id)
            raise

    async def get_signal(self, signal_id: str) -> SignalRecord | None:
        """Get signal by ID."""
        if not self._conn:
            msg = "Repository not initialized"
            raise RuntimeError(msg)

        async with self._conn.execute(
            "SELECT * FROM signals WHERE signal_id = ?", (signal_id,)
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                return self._row_to_signal_record(row)
            return None

    async def get_outcome(self, outcome_id: str) -> OutcomeRecord | None:
        """Get outcome by ID."""
        if not self._conn:
            msg = "Repository not initialized"
            raise RuntimeError(msg)

        async with self._conn.execute(
            "SELECT * FROM outcomes WHERE outcome_id = ?", (outcome_id,)
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                return self._row_to_outcome_record(row)
            return None

    async def get_outcome_by_signal(self, signal_id: str) -> OutcomeRecord | None:
        """Get outcome for a signal."""
        if not self._conn:
            msg = "Repository not initialized"
            raise RuntimeError(msg)

        async with self._conn.execute(
            "SELECT * FROM outcomes WHERE signal_id = ?", (signal_id,)
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                return self._row_to_outcome_record(row)
            return None

    async def get_signals_without_outcome(self, limit: int = 100) -> list[SignalRecord]:
        """Get signals that don't have outcomes yet."""
        if not self._conn:
            msg = "Repository not initialized"
            raise RuntimeError(msg)

        async with self._conn.execute(
            """
            SELECT s.* FROM signals s
            LEFT JOIN outcomes o ON s.signal_id = o.signal_id
            WHERE o.outcome_id IS NULL
            ORDER BY s.created_at DESC
            LIMIT ?
        """,
            (limit,),
        ) as cursor:
            rows = await cursor.fetchall()
            return [self._row_to_signal_record(row) for row in rows]

    async def get_signals_by_strategy(
        self, strategy_id: str, since: datetime | None = None, limit: int = 1000
    ) -> list[SignalRecord]:
        """Get signals for a strategy."""
        if not self._conn:
            msg = "Repository not initialized"
            raise RuntimeError(msg)

        query = "SELECT * FROM signals WHERE strategy_id = ?"
        params: list[Any] = [strategy_id]

        if since:
            query += " AND created_at > ?"
            params.append(since.isoformat())

        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)

        async with self._conn.execute(query, params) as cursor:
            rows = await cursor.fetchall()
            return [self._row_to_signal_record(row) for row in rows]

    async def get_signals_for_analysis(
        self,
        since: datetime,
        min_score: float = 0.0,
        until: datetime | None = None,
    ) -> pl.DataFrame:
        """Get signals as Polars DataFrame for analysis."""
        if not self._conn:
            msg = "Repository not initialized"
            raise RuntimeError(msg)

        query = """
            SELECT s.*, o.result, o.pnl_24h, o.max_profit_pct, o.max_loss_pct
            FROM signals s
            LEFT JOIN outcomes o ON s.signal_id = o.signal_id
            WHERE s.created_at > ? AND s.score >= ?
        """
        params: list[Any] = [since.isoformat(), min_score]
        if until is not None:
            query += " AND s.created_at <= ?"
            params.append(until.isoformat())
        query += """
            ORDER BY s.created_at DESC
        """

        async with self._conn.execute(query, params) as cursor:
            rows = await cursor.fetchall()

        if not rows:
            return pl.DataFrame(schema=SIGNAL_ANALYSIS_SCHEMA)

        # Convert to dicts for Polars
        data = [dict(row) for row in rows]
        return pl.DataFrame(data)

    async def save_config_version(self, config_json: str) -> str:
        """Save config version."""
        if not self._conn:
            msg = "Repository not initialized"
            raise RuntimeError(msg)

        version_id = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")

        try:
            await self._conn.execute(
                """
                INSERT INTO config_versions (version_id, config_json, created_at, is_active)
                VALUES (?, ?, ?, 1)
            """,
                (version_id, config_json, datetime.now(UTC).isoformat()),
            )

            # Deactivate previous versions
            await self._conn.execute(
                """
                UPDATE config_versions SET is_active = 0
                WHERE version_id != ?
            """,
                (version_id,),
            )

            await self._conn.commit()
        except Exception:
            LOG.exception("failed to save config version")
            raise
        return version_id

    async def get_active_config(self) -> str | None:
        """Get active config JSON."""
        if not self._conn:
            msg = "Repository not initialized"
            raise RuntimeError(msg)

        async with self._conn.execute(
            "SELECT config_json FROM config_versions WHERE is_active = 1 LIMIT 1"
        ) as cursor:
            row = await cursor.fetchone()
            return row["config_json"] if row else None

    def _row_to_signal_record(self, row: aiosqlite.Row) -> SignalRecord:
        """Convert DB row to SignalRecord."""
        data = dict(row)
        # Parse JSON fields
        if data.get("features"):
            try:
                data["features"] = json.loads(data["features"])
            except json.JSONDecodeError:
                LOG.exception(
                    "failed to decode features for signal %s",
                    data.get("signal_id"),
                )
                data["features"] = {}
        if data.get("metadata"):
            try:
                data["metadata"] = json.loads(data["metadata"])
            except json.JSONDecodeError:
                LOG.exception(
                    "failed to decode metadata for signal %s",
                    data.get("signal_id"),
                )
                data["metadata"] = {}
        return SignalRecord.from_dict(data)

    def _row_to_outcome_record(self, row: aiosqlite.Row) -> OutcomeRecord:
        """Convert DB row to OutcomeRecord."""
        data = dict(row)
        # Convert integer booleans
        data["hit_tp1"] = bool(data.get("hit_tp1", 0))
        data["hit_tp2"] = bool(data.get("hit_tp2", 0))
        data["hit_sl"] = bool(data.get("hit_sl", 0))
        return OutcomeRecord.from_dict(data)

    # ------------------------------------------------------------------
    # Cooldown methods (replaces SignalCooldownStore)
    # ------------------------------------------------------------------

    async def get_cooldown(self, cooldown_key: str) -> datetime | None:
        """Get last sent time for a cooldown key."""
        if not self._conn:
            msg = "Repository not initialized"
            raise RuntimeError(msg)

        async with self._conn.execute(
            "SELECT last_sent_at FROM cooldowns WHERE cooldown_key = ?", (cooldown_key,)
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                return datetime.fromisoformat(row["last_sent_at"])
            return None

    async def purge_cooldowns_older_than(
        self, *, max_age_minutes: int, now: datetime | None = None
    ) -> int:
        """Delete persisted cooldown rows older than the cleanup age."""
        if not self._conn:
            msg = "Repository not initialized"
            raise RuntimeError(msg)

        now = (now or datetime.now(UTC)).astimezone(UTC)
        cutoff = now - timedelta(minutes=max(1, int(max_age_minutes)))
        cursor = await self._conn.execute(
            "DELETE FROM cooldowns WHERE last_sent_at < ?",
            (cutoff.isoformat(),),
        )
        changed = int(cursor.rowcount or 0)
        await cursor.close()
        await self._conn.commit()
        return changed

    async def set_cooldown(
        self,
        cooldown_key: str,
        sent_at: datetime,
        setup_id: str | None = None,
        symbol: str | None = None,
        cooldown_type: str = "signal_key",
    ) -> None:
        """Set cooldown for a key."""
        if not self._conn:
            msg = "Repository not initialized"
            raise RuntimeError(msg)

        await self._conn.execute(
            """
            INSERT INTO cooldowns (cooldown_key, last_sent_at, setup_id, symbol, cooldown_type)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(cooldown_key) DO UPDATE SET
                last_sent_at = excluded.last_sent_at,
                setup_id = excluded.setup_id,
                symbol = excluded.symbol,
                cooldown_type = excluded.cooldown_type
        """,
            (cooldown_key, sent_at.isoformat(), setup_id, symbol, cooldown_type),
        )
        await self._conn.commit()

    async def is_cooldown_active(
        self, cooldown_key: str, cooldown_minutes: int, now: datetime | None = None
    ) -> bool:
        """Check if cooldown is still active."""
        if cooldown_minutes <= 0:
            return False

        last_sent = await self.get_cooldown(cooldown_key)
        if not last_sent:
            return False

        now = now or datetime.now(UTC)
        return (now - last_sent) < timedelta(minutes=cooldown_minutes)

    # ------------------------------------------------------------------
    # Setup adaptive scoring (replaces setup_score_adjustments)
    # ------------------------------------------------------------------

    async def get_setup_score_adjustment(self, setup_id: str) -> float:
        """Get current score adjustment for a setup.

        The persisted rolling window is kept for fast online updates, but the
        dashboard/outcome table is the stronger source when enough recent
        R-multiple outcomes exist. This prevents positive smart exits from
        being treated as losses and lets badly underperforming setups be
        down-ranked before they keep producing more signals.
        """
        if not self._conn:
            msg = "Repository not initialized"
            raise RuntimeError(msg)

        async with self._conn.execute(
            "SELECT score_adjustment FROM setup_scores WHERE setup_id = ?", (setup_id,)
        ) as cursor:
            row = await cursor.fetchone()
            window_adjustment = 0.0
            if row is not None:
                window_adjustment = float(row["score_adjustment"] or 0.0)

        outcome_adjustment = await self._recent_outcome_score_adjustment(setup_id)
        if outcome_adjustment < 0.0:
            return min(window_adjustment, outcome_adjustment)
        if outcome_adjustment > 0.0:
            return max(window_adjustment, outcome_adjustment)
        return window_adjustment

    async def _recent_outcome_score_adjustment(
        self,
        setup_id: str,
        *,
        last_days: int = 90,
        min_outcomes: int = 3,
    ) -> float:
        if not self._conn:
            msg = "Repository not initialized"
            raise RuntimeError(msg)

        since = datetime.now(UTC) - timedelta(days=last_days)
        async with self._conn.execute(
            """
            SELECT
                COUNT(*) AS total,
                SUM(CASE WHEN was_profitable = 1 THEN 1 ELSE 0 END) AS wins,
                AVG(pnl_r_multiple) AS avg_r_multiple
            FROM signal_outcomes
            WHERE setup_id = ?
              AND COALESCE(closed_at, created_at) >= ?
              AND result NOT IN (
                  'expired',
                  'expired_active',
                  'expired_pending',
                  'risk_monitor_exit',
                  'smart_exit',
                  'unactivated_close',
                  'superseded'
              )
              AND (
                  activated_at IS NOT NULL
                  OR result IN (
                      'tp1_hit',
                      'tp2_hit',
                      'stop_loss',
                      'breakeven_stop',
                      'trailing_stop',
                      'emergency_exit',
                      'ambiguous_exit'
                  )
              )
            """,
            (setup_id, since.isoformat()),
        ) as cursor:
            row = await cursor.fetchone()

        total = 0
        wins = 0
        avg_r = 0.0
        if row is not None:
            total = int(row["total"] or 0)
            wins = int(row["wins"] or 0)
            avg_r = float(row["avg_r_multiple"] or 0.0)

        if total < min_outcomes:
            return 0.0

        win_rate = wins / total if total > 0 else 0.0
        if not math.isfinite(avg_r):
            avg_r = 0.0

        if total >= 3 and wins == 0 and avg_r <= -0.35:
            return -0.22
        if avg_r <= -0.35 or (avg_r <= -0.15 and win_rate < 0.40):
            return -0.18
        if avg_r < 0.0 and win_rate < 0.35:
            return -0.10
        if avg_r >= 0.35 and win_rate >= 0.50:
            return 0.06
        if avg_r >= 0.15 and win_rate >= 0.55:
            return 0.03
        return 0.0

    async def record_setup_outcome(
        self,
        setup_id: str,
        outcome: str,
        *,
        pnl_r_multiple: float | None = None,
        was_profitable: bool | None = None,
        window_size: int = 20,
        min_outcomes: int = 3,
        penalty: float = -0.05,
        bonus: float = 0.03,
        low_win_rate: float = 0.40,
        high_win_rate: float = 0.60,
    ) -> float:
        """Record outcome and return new score adjustment.

        Replaces SignalCooldownStore.record_outcome()
        """
        if not self._conn:
            msg = "Repository not initialized"
            raise RuntimeError(msg)

        # Get current window
        async with self._conn.execute(
            "SELECT outcome_window FROM setup_scores WHERE setup_id = ?", (setup_id,)
        ) as cursor:
            row = await cursor.fetchone()
            window: list[Any] = []
            if row is not None and row["outcome_window"]:
                try:
                    window = json.loads(row["outcome_window"])
                except json.JSONDecodeError:
                    LOG.exception("failed to decode outcome window for setup %s", setup_id)
                    window = []

        # Add new outcome. New entries include R data; older string-only
        # entries remain supported for existing SQLite state.
        entry: str | dict[str, Any]
        if pnl_r_multiple is None and was_profitable is None:
            entry = outcome
        else:
            r_value = None
            if pnl_r_multiple is not None:
                try:
                    parsed = float(pnl_r_multiple)
                except (TypeError, ValueError):
                    parsed = math.nan
                if math.isfinite(parsed):
                    r_value = round(parsed, 6)
            entry = {
                "outcome": str(outcome),
                "pnl_r_multiple": r_value,
                "was_profitable": bool(was_profitable)
                if was_profitable is not None
                else (r_value is not None and r_value > 0.0),
            }
        window.append(entry)
        if len(window) > window_size:
            window = window[-window_size:]

        # Calculate adjustment
        win_reasons = {"tp1_hit", "tp2_hit", "tp3_hit", "partial_tp"}
        adjustment = 0.0
        if len(window) >= min_outcomes:
            wins = sum(1 for item in window if self._setup_outcome_is_win(item, win_reasons))
            win_rate = wins / len(window)
            r_values = [
                r_value
                for item in window
                if (r_value := self._setup_outcome_r_multiple(item)) is not None
            ]
            avg_r = sum(r_values) / len(r_values) if r_values else 0.0
            if len(window) >= 3 and wins == 0 and avg_r <= -0.35:
                adjustment = min(penalty * 4.0, -0.20)
            elif avg_r <= -0.35 or (win_rate < low_win_rate and avg_r < 0.0):
                adjustment = min(penalty * 3.0, -0.15)
            elif win_rate < low_win_rate:
                adjustment = penalty
            elif win_rate > high_win_rate or (avg_r >= 0.35 and win_rate >= 0.50):
                adjustment = bonus

        # Save
        await self._conn.execute(
            """
            INSERT INTO setup_scores (setup_id, score_adjustment, outcome_window, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(setup_id) DO UPDATE SET
                score_adjustment = excluded.score_adjustment,
                outcome_window = excluded.outcome_window,
                updated_at = excluded.updated_at
        """,
            (
                setup_id,
                adjustment,
                json.dumps(window),
                datetime.now(UTC).isoformat(),
            ),
        )
        await self._conn.commit()

        return adjustment

    @staticmethod
    def _setup_outcome_is_win(item: Any, win_reasons: set[str]) -> bool:
        if isinstance(item, dict):
            profitable = item.get("was_profitable")
            if isinstance(profitable, bool):
                return profitable
            r_value = MemoryRepository._setup_outcome_r_multiple(item)
            if r_value is not None:
                return r_value > 0.0
            return str(item.get("outcome") or "") in win_reasons
        return str(item) in win_reasons

    @staticmethod
    def _setup_outcome_r_multiple(item: Any) -> float | None:
        if not isinstance(item, dict):
            return None
        raw = item.get("pnl_r_multiple")
        if raw is None:
            return None
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return None
        return value if math.isfinite(value) else None

    # ------------------------------------------------------------------
    # Active signal tracking (replaces SignalTrackingStore)
    # ------------------------------------------------------------------

    async def save_active_signal(self, signal_data: dict[str, Any]) -> None:
        """Save or update active signal.

        signal_data must contain: tracking_id, tracking_ref, signal_key, symbol, setup_id, direction
        """
        if not self._conn:
            msg = "Repository not initialized"
            raise RuntimeError(msg)

        # Basic validation
        required = {"tracking_id", "tracking_ref", "signal_key", "symbol", "setup_id", "direction"}
        missing = required - set(signal_data.keys())
        if missing:
            msg = f"missing required fields in signal_data: {missing}"
            raise ValueError(msg)

        # Build columns and values
        columns = [
            "tracking_id",
            "tracking_ref",
            "signal_key",
            "symbol",
            "setup_id",
            "direction",
            "timeframe",
            "created_at",
            "pending_expires_at",
            "active_expires_at",
            "entry_low",
            "entry_high",
            "entry_mid",
            "initial_stop",
            "stop",
            "take_profit_1",
            "take_profit_2",
            "take_profit_3",
            "valid_until",
            "scale_weights",
            "ttl_bars",
            "single_target_mode",
            "target_integrity_status",
            "score",
            "risk_reward",
            "reasons",
            "signal_message_id",
            "bias_4h",
            "quote_volume",
            "spread_bps",
            "atr_pct",
            "orderflow_delta_ratio",
            "status",
            "activated_at",
            "activation_price",
            "tp1_hit_at",
            "tp2_hit_at",
            "stop_price",
            "tp1_price",
            "tp2_price",
            "tp3_price",
            "last_checked_at",
            "last_price",
            "closed_at",
            "close_reason",
            "close_price",
        ]

        values = []
        for col in columns:
            val = signal_data.get(col)
            if col == "reasons" and isinstance(val, (list, tuple)):
                val = json.dumps(list(val))
            if col == "scale_weights" and isinstance(val, (list, tuple)):
                val = json.dumps(list(val))
            values.append(val)

        placeholders = ", ".join(["?"] * len(columns))
        updates = ", ".join([f"{col} = excluded.{col}" for col in columns if col != "tracking_id"])

        try:
            await self._conn.execute(
                f"""
                INSERT INTO active_signals ({", ".join(columns)})
                VALUES ({placeholders})
                ON CONFLICT(tracking_id) DO UPDATE SET {updates}
            """,
                values,
            )
            await self._conn.commit()
        except Exception:
            LOG.exception("failed to save active signal %s", signal_data.get("tracking_id"))
            raise

    async def get_active_signals(
        self,
        symbol: str | None = None,
        status: str | None = None,
        *,
        include_closed: bool = False,
    ) -> list[dict[str, Any]]:
        """Get active signals with optional filtering."""
        if not self._conn:
            msg = "Repository not initialized"
            raise RuntimeError(msg)

        query = "SELECT * FROM active_signals WHERE 1=1"
        params: list[Any] = []

        if not include_closed and status is None:
            query += " AND status IN ('pending', 'active')"
        if symbol:
            query += " AND symbol = ?"
            params.append(symbol)
        if status:
            query += " AND status = ?"
            params.append(status)

        query += " ORDER BY created_at DESC"

        try:
            async with self._conn.execute(query, params) as cursor:
                rows = await cursor.fetchall()
                result = []
                for row in rows:
                    data = dict(row)
                    if data.get("reasons"):
                        try:
                            data["reasons"] = json.loads(data["reasons"])
                        except json.JSONDecodeError:
                            LOG.exception(
                                "failed to decode reasons for signal %s", data.get("tracking_id")
                            )
                            data["reasons"] = []
                    if data.get("scale_weights"):
                        try:
                            data["scale_weights"] = tuple(json.loads(data["scale_weights"]))
                        except (json.JSONDecodeError, TypeError, ValueError):
                            LOG.exception(
                                "failed to decode scale_weights for signal %s",
                                data.get("tracking_id"),
                            )
                            data["scale_weights"] = (0.5, 0.3, 0.2)
                    result.append(data)
                return result
        except Exception:
            LOG.exception("failed to get active signals")
            return []

    async def close_active_signal(
        self,
        tracking_id: str,
        close_reason: str,
        close_price: float | None = None,
        closed_at: datetime | None = None,
    ) -> None:
        """Close an active signal."""
        if not self._conn:
            msg = "Repository not initialized"
            raise RuntimeError(msg)

        closed_at = closed_at or datetime.now(UTC)

        try:
            await self._conn.execute(
                """
                UPDATE active_signals
                SET status = 'closed',
                    close_reason = ?,
                    close_price = ?,
                    closed_at = ?
                WHERE tracking_id = ?
            """,
                (close_reason, close_price, closed_at.isoformat(), tracking_id),
            )
            await self._conn.commit()
        except Exception:
            LOG.exception("failed to close active signal %s", tracking_id)
            raise

    async def expire_open_signals_older_than(
        self, *, max_age_minutes: int, now: datetime | None = None
    ) -> int:
        """Close pending/active signals older than a hard runtime age limit."""
        if not self._conn:
            msg = "Repository not initialized"
            raise RuntimeError(msg)

        now = (now or datetime.now(UTC)).astimezone(UTC)
        cutoff = now - timedelta(minutes=max(1, int(max_age_minutes)))
        cursor = await self._conn.execute(
            """
            UPDATE active_signals
            SET status = 'closed',
                close_reason = 'expired',
                close_price = COALESCE(last_price, activation_price, entry_mid, close_price),
                closed_at = ?
            WHERE status IN ('pending', 'active')
              AND created_at < ?
            """,
            (now.isoformat(), cutoff.isoformat()),
        )
        changed = int(cursor.rowcount or 0)
        await cursor.close()
        await self._conn.commit()
        if changed:
            await self.increment_tracking_stats(expired=changed)
            LOG.info(
                "signals_expired",
                extra={"count": changed, "age_hours": max(1, int(max_age_minutes)) / 60.0},
            )
        return changed

    async def update_signal_status(
        self,
        tracking_id: str,
        status: str,
        activation_price: float | None = None,
        activated_at: datetime | None = None,
    ) -> None:
        """Update signal status (e.g., pending -> active)."""
        if not self._conn:
            msg = "Repository not initialized"
            raise RuntimeError(msg)

        try:
            if status == "active" and activated_at:
                await self._conn.execute(
                    """
                    UPDATE active_signals
                    SET status = ?, activation_price = ?, activated_at = ?
                    WHERE tracking_id = ?
                """,
                    (status, activation_price, activated_at.isoformat(), tracking_id),
                )
            else:
                await self._conn.execute(
                    "UPDATE active_signals SET status = ? WHERE tracking_id = ?",
                    (status, tracking_id),
                )
            await self._conn.commit()
        except Exception:
            LOG.exception("failed to update signal status for %s", tracking_id)
            raise

    async def get_tracking_stats(self) -> dict[str, int]:
        """Return tracking lifecycle counters."""
        if not self._conn:
            msg = "Repository not initialized"
            raise RuntimeError(msg)

        async with self._conn.execute(
            """
            SELECT signals_sent, activated, tp1_hit, tp2_hit, stop_loss, expired, ambiguous_exit
            FROM tracking_stats
            WHERE id = 1
            """
        ) as cursor:
            row = await cursor.fetchone()

        stats = (
            dict(row)
            if row
            else {
                "signals_sent": 0,
                "activated": 0,
                "tp1_hit": 0,
                "tp2_hit": 0,
                "stop_loss": 0,
                "expired": 0,
                "ambiguous_exit": 0,
            }
        )
        async with self._conn.execute(
            "SELECT COUNT(*) AS active_count FROM active_signals "
            "WHERE status IN ('pending', 'active')"
        ) as cursor:
            active_row = await cursor.fetchone()
        stats["active"] = int(active_row["active_count"]) if active_row else 0
        stats["ambiguous"] = int(stats.pop("ambiguous_exit", 0))
        return {key: int(value) for key, value in stats.items()}

    async def increment_tracking_stats(self, **deltas: int) -> None:
        """Increment one or more tracking counters."""
        if not self._conn:
            msg = "Repository not initialized"
            raise RuntimeError(msg)

        allowed = {
            "signals_sent",
            "activated",
            "tp1_hit",
            "tp2_hit",
            "stop_loss",
            "expired",
            "ambiguous_exit",
        }
        updates: list[str] = []
        params: list[int] = []
        for key, delta in deltas.items():
            if key not in allowed or delta == 0:
                continue
            updates.append(f"{key} = {key} + ?")
            params.append(int(delta))

        if not updates:
            return

        params.append(1)
        try:
            await self._conn.execute(
                f"UPDATE tracking_stats SET {', '.join(updates)} WHERE id = ?",
                params,
            )
            await self._conn.commit()
        except Exception:
            LOG.exception("failed to increment tracking stats")
            raise

    async def save_signal_outcome(self, outcome_data: dict[str, Any]) -> None:
        """Persist a completed tracked-signal outcome."""
        await self.save_signal_outcomes_batch([outcome_data])

    async def save_signal_outcomes_batch(self, outcomes_data: list[dict[str, Any]]) -> None:
        """Persist completed tracked-signal outcomes in batch."""
        if not self._conn:
            msg = "Repository not initialized"
            raise RuntimeError(msg)
        if not outcomes_data:
            return

        # Basic validation
        required = {
            "tracking_id",
            "tracking_ref",
            "symbol",
            "setup_id",
            "direction",
            "timeframe",
            "created_at",
        }
        for i, item in enumerate(outcomes_data):
            missing = required - set(item.keys())
            if missing:
                msg = f"missing required fields in outcomes_data at index {i}: {missing}"
                raise ValueError(msg)

        query = """
            INSERT INTO signal_outcomes (
                tracking_id, signal_id, tracking_ref, symbol, setup_id, direction, timeframe,
                created_at, activated_at, closed_at, entry_price, exit_price, result,
                pnl_pct, pnl_r_multiple, max_profit_pct, max_loss_pct, mae, mfe,
                time_to_entry_min, time_to_exit_min, features, was_profitable,
                llm_was_correct, setup_quality
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(tracking_id) DO UPDATE SET
                signal_id = excluded.signal_id,
                tracking_ref = excluded.tracking_ref,
                symbol = excluded.symbol,
                setup_id = excluded.setup_id,
                direction = excluded.direction,
                timeframe = excluded.timeframe,
                created_at = excluded.created_at,
                activated_at = excluded.activated_at,
                closed_at = excluded.closed_at,
                entry_price = excluded.entry_price,
                exit_price = excluded.exit_price,
                result = excluded.result,
                pnl_pct = excluded.pnl_pct,
                pnl_r_multiple = excluded.pnl_r_multiple,
                max_profit_pct = excluded.max_profit_pct,
                max_loss_pct = excluded.max_loss_pct,
                mae = excluded.mae,
                mfe = excluded.mfe,
                time_to_entry_min = excluded.time_to_entry_min,
                time_to_exit_min = excluded.time_to_exit_min,
                features = excluded.features,
                was_profitable = excluded.was_profitable,
                llm_was_correct = excluded.llm_was_correct,
                setup_quality = excluded.setup_quality
        """
        rows: list[tuple[Any, ...]] = []
        for item in outcomes_data:
            llm_was_correct = item.get("llm_was_correct")
            rows.append(
                (
                    item["tracking_id"],
                    item.get("signal_id", item["tracking_id"]),
                    item["tracking_ref"],
                    item["symbol"],
                    item["setup_id"],
                    item["direction"],
                    item["timeframe"],
                    item["created_at"],
                    item.get("activated_at"),
                    item.get("closed_at"),
                    item.get("entry_price"),
                    item.get("exit_price"),
                    item.get("result", ""),
                    item.get("pnl_pct", 0.0),
                    item.get("pnl_r_multiple", 0.0),
                    item.get("max_profit_pct", 0.0),
                    item.get("max_loss_pct", 0.0),
                    item.get("mae", 0.0),
                    item.get("mfe", 0.0),
                    item.get("time_to_entry_min", 0),
                    item.get("time_to_exit_min", 0),
                    json.dumps(item.get("features", {})),
                    int(bool(item.get("was_profitable", False))),
                    None if llm_was_correct is None else int(bool(llm_was_correct)),
                    item.get("setup_quality", "neutral"),
                )
            )
        try:
            await self._conn.executemany(query, rows)
            await self._conn.commit()
        except Exception:
            LOG.exception("failed to save signal outcomes batch")
            raise

    async def get_setup_stats(
        self,
        setup_id: str | None = None,
        *,
        last_days: int | None = 90,
        since: datetime | str | None = None,
    ) -> list[dict[str, Any]]:
        """Aggregate tracked-signal outcome performance by setup."""
        if not self._conn:
            msg = "Repository not initialized"
            raise RuntimeError(msg)

        query = """
            SELECT
                setup_id,
                SUM(
                    CASE
                        WHEN was_profitable = 1
                             AND result NOT IN (
                                 'expired_pending',
                                 'expired',
                                 'expired_active',
                                 'risk_monitor_exit',
                                 'smart_exit',
                                 'unactivated_close',
                                 'superseded'
                             )
                        THEN 1 ELSE 0
                    END
                ) AS wins,
                SUM(CASE WHEN result = 'stop_loss' THEN 1 ELSE 0 END) AS losses,
                SUM(
                    CASE
                        WHEN result IN (
                            'expired_pending',
                            'expired',
                            'expired_active',
                            'risk_monitor_exit',
                            'smart_exit',
                            'unactivated_close',
                            'superseded'
                        ) THEN 0
                        WHEN activated_at IS NOT NULL THEN 1
                        WHEN result IN (
                            'tp1_hit',
                            'tp2_hit',
                            'stop_loss',
                            'breakeven_stop',
                            'trailing_stop',
                            'emergency_exit',
                            'ambiguous_exit'
                        ) THEN 1
                        ELSE 0
                    END
                ) AS total,
                AVG(
                    CASE
                        WHEN result IN (
                            'expired_pending',
                            'expired',
                            'expired_active',
                            'risk_monitor_exit',
                            'smart_exit',
                            'unactivated_close',
                            'superseded'
                        ) THEN NULL
                        WHEN activated_at IS NOT NULL THEN pnl_r_multiple
                        WHEN result IN (
                            'tp1_hit',
                            'tp2_hit',
                            'stop_loss',
                            'breakeven_stop',
                            'trailing_stop',
                            'emergency_exit',
                            'ambiguous_exit'
                        ) THEN pnl_r_multiple
                        ELSE NULL
                    END
                ) AS avg_r_multiple,
                AVG(
                    CASE
                        WHEN result IN (
                            'expired_pending',
                            'expired',
                            'expired_active',
                            'risk_monitor_exit',
                            'smart_exit',
                            'unactivated_close',
                            'superseded'
                        ) THEN NULL
                        WHEN activated_at IS NOT NULL THEN pnl_pct
                        WHEN result IN (
                            'tp1_hit',
                            'tp2_hit',
                            'stop_loss',
                            'breakeven_stop',
                            'trailing_stop',
                            'emergency_exit',
                            'ambiguous_exit'
                        ) THEN pnl_pct
                        ELSE NULL
                    END
                ) AS avg_pnl_pct
            FROM signal_outcomes
            WHERE 1 = 1
        """
        params: list[Any] = []
        if setup_id:
            query += " AND setup_id = ?"
            params.append(setup_id)
        if since is not None:
            since_iso = since.isoformat() if isinstance(since, datetime) else str(since)
            query += " AND COALESCE(closed_at, created_at) >= ?"
            params.append(since_iso)
        elif last_days is not None:
            since = datetime.now(UTC) - timedelta(days=last_days)
            query += " AND COALESCE(closed_at, created_at) >= ?"
            params.append(since.isoformat())
        query += " GROUP BY setup_id ORDER BY total DESC, setup_id ASC"

        async with self._conn.execute(query, params) as cursor:
            rows = await cursor.fetchall()

        result: list[dict[str, Any]] = []
        for row in rows:
            total = int(row["total"] or 0)
            wins = int(row["wins"] or 0)
            losses = int(row["losses"] or 0)
            result.append(
                {
                    "setup_id": row["setup_id"],
                    "wins": wins,
                    "losses": losses,
                    "total": total,
                    "win_rate": (wins / total) if total > 0 else 0.0,
                    "avg_r_multiple": float(row["avg_r_multiple"] or 0.0),
                    "avg_pnl_pct": float(row["avg_pnl_pct"] or 0.0),
                }
            )
        return result

    async def get_signal_outcomes(
        self,
        *,
        setup_id: str | None = None,
        symbol: str | None = None,
        result: str | None = None,
        last_days: int | None = 30,
        since: datetime | str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """Return persisted tracked-signal outcomes."""
        if not self._conn:
            msg = "Repository not initialized"
            raise RuntimeError(msg)

        query = "SELECT * FROM signal_outcomes WHERE 1 = 1"
        params: list[Any] = []
        if setup_id:
            query += " AND setup_id = ?"
            params.append(setup_id)
        if symbol:
            query += " AND symbol = ?"
            params.append(symbol)
        if result:
            query += " AND result = ?"
            params.append(result)
        if since is not None:
            since_iso = since.isoformat() if isinstance(since, datetime) else str(since)
            query += " AND COALESCE(closed_at, created_at) >= ?"
            params.append(since_iso)
        elif last_days is not None:
            since = datetime.now(UTC) - timedelta(days=last_days)
            query += " AND COALESCE(closed_at, created_at) >= ?"
            params.append(since.isoformat())

        query += " ORDER BY COALESCE(closed_at, created_at) DESC"
        if limit is not None:
            query += " LIMIT ?"
            params.append(limit)

        async with self._conn.execute(query, params) as cursor:
            rows = await cursor.fetchall()

        result_rows: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            raw_features = item.get("features")
            if raw_features:
                try:
                    item["features"] = json.loads(raw_features)
                except json.JSONDecodeError:
                    item["features"] = {}
            else:
                item["features"] = {}
            item["was_profitable"] = bool(item.get("was_profitable", 0))
            llm_was_correct = item.get("llm_was_correct")
            item["llm_was_correct"] = None if llm_was_correct is None else bool(llm_was_correct)
            result_rows.append(item)
        return result_rows

    async def get_cooldown_count(self) -> int:
        """Return number of persisted cooldown entries."""
        if not self._conn:
            msg = "Repository not initialized"
            raise RuntimeError(msg)
        async with self._conn.execute("SELECT COUNT(*) AS count FROM cooldowns") as cursor:
            row = await cursor.fetchone()
        return int(row["count"]) if row else 0

    async def cleanup_signal_outcomes_before(self, cutoff_iso: str) -> int:
        """Delete old outcomes and return deleted row count."""
        if not self._conn:
            msg = "Repository not initialized"
            raise RuntimeError(msg)
        cursor = await self._conn.execute(
            """
            DELETE FROM signal_outcomes
            WHERE COALESCE(closed_at, created_at) < ?
              AND result NOT IN ('active', 'pending')
            """,
            (cutoff_iso,),
        )
        await self._conn.commit()
        return int(cursor.rowcount or 0)
