"""SQLite + parquet persistence for signals and outcomes."""

# active_signals / signal_outcomes = primary tracking model (written by tracking.py lifecycle).
# Legacy signals/outcomes tables removed in migration 9 (Phase H).

from __future__ import annotations

import json
import logging
import sqlite3
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import aiosqlite
import polars as pl

from bot.persistence.repository._analytics import (
    AnalyticsMixin as _MemoryRepositoryBases,
)
from bot.persistence.repository._analytics import (
    fetch_setup_stats_rows,
    fetch_signal_outcome_rows,
)
from bot.persistence.repository._schema import REPOSITORY_CORE_DDL
from engine.errors import DEFENSIVE_EXC
from engine.market.data import read_market_data_cache, write_market_data_cache

from ...migrations import migrate_db

__all__ = [
    "MemoryRepository",
    "fetch_setup_stats_rows",
    "fetch_signal_outcome_rows",
]
from .schema import (
    SIGNAL_ANALYSIS_SCHEMA,
    OutcomeRecord,
    SignalRecord,
)

LOG = logging.getLogger("bot.persistence.repository")
_CACHE_MISS = object()


_ACTIVE_SIGNALS_OPTIONAL_COLUMNS: dict[str, str] = {
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
    "entry_zone_touched_at": "TEXT",
    "entry_confirm_pending_at": "TEXT",
    "last_lifecycle_note": "TEXT",
    "trailing_stop": "REAL",
    # Market vs limit entry semantics must survive into the lifecycle: without this
    # column the value was silently dropped on persist and every signal reloaded as
    # "limit", collapsing the entire market/limit split inside tracking/activation.
    "entry_order_type": "TEXT DEFAULT 'limit'",
    "entry_tf": "TEXT DEFAULT ''",
    "pattern_tf": "TEXT DEFAULT ''",
    "context_tfs": "TEXT DEFAULT '[]'",
}

_SIGNAL_OUTCOMES_OPTIONAL_COLUMNS: dict[str, str] = {
    "entry_tf": "TEXT DEFAULT ''",
    "pattern_tf": "TEXT DEFAULT ''",
    "context_tfs": "TEXT DEFAULT '[]'",
}


class MemoryRepository(_MemoryRepositoryBases):
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
        self._cooldown_cache: dict[str, datetime] = {}
        self._cooldown_cache_mono: dict[str, float] = {}
        self._cooldown_cache_ttl_s = 30.0

    def _require_conn(self) -> aiosqlite.Connection:
        conn = self._conn
        if conn is None:
            msg = "Repository not initialized"
            raise RuntimeError(msg)
        return conn

    async def initialize(self, *, skip_ddl: bool = False) -> None:
        """Initialize database connection and tables.

        Args:
            skip_ddl: When True, open connection but skip DDL + migration.
                Use for read-only snapshots that must not contend for write locks.
        """
        self._conn = await aiosqlite.connect(self._db_path, timeout=30.0)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA busy_timeout=30000")
        await self._conn.execute("PRAGMA journal_mode=WAL")
        if skip_ddl:
            LOG.debug("Memory repository opened (skip_ddl) at %s", self._db_path)
            return
        # Create tables
        await self._conn.executescript(REPOSITORY_CORE_DDL)

        await self._ensure_table_columns(
            "active_signals",
            _ACTIVE_SIGNALS_OPTIONAL_COLUMNS,
        )
        await self._ensure_extended_tables(run_column_migrations=True)
        await self._ensure_table_columns("active_signals", _ACTIVE_SIGNALS_OPTIONAL_COLUMNS)
        await self._ensure_table_columns("signal_outcomes", _SIGNAL_OUTCOMES_OPTIONAL_COLUMNS)
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
                intelligence_json TEXT DEFAULT '{}',
                telegram_html TEXT DEFAULT '',
                display_snapshot_json TEXT DEFAULT '{}'
            )
        """)
        await self._ensure_table_columns(
            "market_context",
            {
                "telegram_html": "TEXT DEFAULT ''",
                "display_snapshot_json": "TEXT DEFAULT '{}'",
            },
        )
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
        telegram_html: str | None = None,
        display_snapshot: dict[str, Any] | None = None,
    ) -> None:
        """Update market context in SQLite."""
        conn = self._require_conn()
        await self._ensure_extended_tables()

        existing_html = ""
        existing_display = "{}"
        async with conn.execute(
            "SELECT telegram_html, display_snapshot_json FROM market_context WHERE id = 1"
        ) as cursor:
            row = await cursor.fetchone()
            if row is not None:
                existing_html = str(row["telegram_html"] or "")
                existing_display = str(row["display_snapshot_json"] or "{}")

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
                intelligence_json,
                telegram_html,
                display_snapshot_json
            )
            VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                telegram_html if telegram_html is not None else existing_html,
                json.dumps(display_snapshot or {}, ensure_ascii=True)
                if display_snapshot is not None
                else existing_display,
            ),
        )
        await conn.commit()

    @staticmethod
    def _market_context_age_seconds(updated_at: object | None) -> float | None:
        if not updated_at:
            return None
        try:
            ts = datetime.fromisoformat(str(updated_at))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=UTC)
            return max(0.0, (datetime.now(UTC) - ts).total_seconds())
        except (TypeError, ValueError):
            return None

    async def get_market_context(self) -> dict[str, Any]:
        """Get current market context."""
        conn = self._require_conn()
        await self._ensure_extended_tables()

        async with conn.execute("SELECT * FROM market_context WHERE id = 1") as cursor:
            row = await cursor.fetchone()
            if row:
                row_dict: dict[str, Any] = dict(row)
                intelligence_snapshot: dict[str, Any] = {}
                raw_intelligence = row_dict.get("intelligence_json")
                benchmark_context: dict[str, Any] = {}
                raw_benchmarks = row_dict.get("benchmark_context_json")
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
                display_snapshot: dict[str, Any] = {}
                raw_display = row_dict.get("display_snapshot_json")
                if raw_display:
                    try:
                        parsed_display = json.loads(raw_display)
                    except json.JSONDecodeError:
                        parsed_display = {}
                    if isinstance(parsed_display, dict):
                        display_snapshot = parsed_display
                return {
                    "btc_bias": row_dict["btc_bias"],
                    "eth_bias": row_dict["eth_bias"],
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
                    "altcoin_season_index": float(row_dict["altcoin_season_index"])
                    if "altcoin_season_index" in row_dict
                    and row_dict["altcoin_season_index"] is not None
                    else 50.0,
                    "btc_phase": row_dict.get("btc_phase", "sideways"),
                    "high_funding_symbols": json.loads(row_dict["high_funding_symbols"]),
                    "low_funding_symbols": json.loads(row_dict["low_funding_symbols"]),
                    "updated_at": row_dict["updated_at"],
                    "market_regime": row_dict.get("market_regime", "unknown"),
                    "market_regime_confirmed": bool(row_dict["market_regime_confirmed"])
                    if "market_regime_confirmed" in row_dict
                    else False,
                    "macro_risk_mode": row_dict.get("macro_risk_mode", "normal"),
                    "benchmark_context": benchmark_context,
                    "intelligence_snapshot": intelligence_snapshot,
                    "telegram_html": str(row_dict.get("telegram_html") or ""),
                    "display_snapshot": display_snapshot,
                    "market_context_age_seconds": self._market_context_age_seconds(
                        row_dict.get("updated_at")
                    ),
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
                "telegram_html": "",
                "display_snapshot": {},
            }

    async def close(self) -> None:
        """Close database connection."""
        if self._conn:
            await self._conn.close()
            self._conn = None

    async def save_signal(self, record: SignalRecord) -> None:
        """Legacy API - no longer writes to ``signals`` (Phase E). Use ``save_active_signal``."""
        record.validate()

    async def save_outcome(self, record: OutcomeRecord, *, commit: bool = True) -> None:
        """Legacy API - no longer writes to ``outcomes`` (Phase E). Use ``signal_outcomes`` path."""
        record.validate()
        del commit

    async def get_signal(self, signal_id: str) -> SignalRecord | None:
        """Legacy API shim — reads from ``active_signals`` by tracking_id."""
        if not self._conn:
            msg = "Repository not initialized"
            raise RuntimeError(msg)

        async with self._conn.execute(
            "SELECT * FROM active_signals WHERE tracking_id = ?",
            (signal_id,),
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                return self._active_row_to_signal_record(row)
            return None

    async def get_outcome(self, outcome_id: str) -> OutcomeRecord | None:
        """Legacy API shim — reads from ``signal_outcomes`` by tracking_id."""
        if not self._conn:
            msg = "Repository not initialized"
            raise RuntimeError(msg)

        async with self._conn.execute(
            "SELECT * FROM signal_outcomes WHERE tracking_id = ?",
            (outcome_id,),
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                return self._signal_outcome_row_to_outcome_record(row)
            return None

    async def get_outcome_by_signal(self, signal_id: str) -> OutcomeRecord | None:
        """Legacy API shim — reads outcome for a tracked signal id."""
        return await self.get_outcome(signal_id)

    async def get_tp1_remap_candidates(self, *, limit: int = 200) -> list[dict[str, Any]]:
        """Closed rows where TP1 was marked but outcomes still show expired/BE."""
        if not self._conn:
            msg = "Repository not initialized"
            raise RuntimeError(msg)

        async with self._conn.execute(
            """
            SELECT a.*
            FROM active_signals a
            INNER JOIN signal_outcomes o ON a.tracking_id = o.tracking_id
            WHERE a.status = 'closed'
              AND a.activated_at IS NOT NULL
              AND (
                    a.tp1_hit_at IS NOT NULL
                    OR a.close_reason IN ('tp1_hit', 'breakeven_stop')
                  )
              AND o.result IN ('expired_active', 'breakeven_stop')
            ORDER BY a.closed_at DESC
            LIMIT ?
            """,
            (max(1, int(limit)),),
        ) as cursor:
            rows = await cursor.fetchall()
            result: list[dict[str, Any]] = []
            for row in rows:
                data = dict(row)
                if data.get("reasons"):
                    try:
                        data["reasons"] = json.loads(data["reasons"])
                    except json.JSONDecodeError:
                        data["reasons"] = []
                if data.get("scale_weights"):
                    try:
                        data["scale_weights"] = json.loads(data["scale_weights"])
                    except json.JSONDecodeError:
                        data["scale_weights"] = (0.5, 0.3, 0.2)
                result.append(data)
            return result

    async def get_signals_without_outcome(self, limit: int = 100) -> list[SignalRecord]:
        """Closed active rows missing ``signal_outcomes`` (reconcile backlog)."""
        if not self._conn:
            msg = "Repository not initialized"
            raise RuntimeError(msg)

        async with self._conn.execute(
            """
            SELECT a.*
            FROM active_signals a
            LEFT JOIN signal_outcomes o ON a.tracking_id = o.tracking_id
            WHERE a.status = 'closed' AND o.tracking_id IS NULL
            ORDER BY a.created_at DESC
            LIMIT ?
        """,
            (limit,),
        ) as cursor:
            rows = await cursor.fetchall()
            return [self._active_row_to_signal_record(row) for row in rows]

    async def get_signals_by_strategy(
        self, strategy_id: str, since: datetime | None = None, limit: int = 1000
    ) -> list[SignalRecord]:
        """Read tracked signals for a setup from ``active_signals``."""
        if not self._conn:
            msg = "Repository not initialized"
            raise RuntimeError(msg)

        query = "SELECT * FROM active_signals WHERE setup_id = ?"
        params: list[Any] = [strategy_id]

        if since:
            query += " AND created_at > ?"
            params.append(since.isoformat())

        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)

        async with self._conn.execute(query, params) as cursor:
            rows = await cursor.fetchall()
            return [self._active_row_to_signal_record(row) for row in rows]

    async def get_signals_for_analysis(
        self,
        since: datetime,
        min_score: float = 0.0,
        until: datetime | None = None,
    ) -> pl.DataFrame:
        """Get tracked signal outcomes as Polars DataFrame for analysis."""
        if not self._conn:
            msg = "Repository not initialized"
            raise RuntimeError(msg)

        query = """
            SELECT
                o.tracking_id AS signal_id,
                o.symbol,
                o.setup_id AS strategy_id,
                o.direction,
                o.entry_price,
                o.exit_price,
                o.result,
                o.pnl_pct AS pnl_24h,
                o.max_profit_pct,
                o.max_loss_pct,
                o.score,
                o.created_at
            FROM signal_outcomes o
            WHERE o.created_at > ? AND COALESCE(o.score, 0) >= ?
        """
        params: list[Any] = [since.isoformat(), min_score]
        if until is not None:
            query += " AND o.created_at <= ?"
            params.append(until.isoformat())
        query += " ORDER BY o.created_at DESC"

        async with self._conn.execute(query, params) as cursor:
            rows = await cursor.fetchall()

        if not rows:
            return pl.DataFrame(schema=SIGNAL_ANALYSIS_SCHEMA)

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

    @staticmethod
    def _active_row_to_signal_record(row: aiosqlite.Row) -> SignalRecord:
        data = dict(row)
        entry_mid = float(data.get("entry_mid") or 0.0)
        mapped = {
            "signal_id": data.get("tracking_id"),
            "symbol": data.get("symbol"),
            "strategy_id": data.get("setup_id"),
            "direction": data.get("direction"),
            "entry_price": data.get("activation_price") or entry_mid,
            "stop_loss": data.get("stop"),
            "take_profit_1": data.get("take_profit_1"),
            "take_profit_2": data.get("take_profit_2"),
            "score": data.get("score") or 0.0,
            "created_at": data.get("created_at"),
            "timeframe": data.get("timeframe") or "15m",
            "atr_pct": data.get("atr_pct") or 0.0,
            "spread_bps": data.get("spread_bps") or 0.0,
            "funding_rate": data.get("funding_rate"),
            "features": {},
            "metadata": {"status": data.get("status")},
        }
        return SignalRecord.from_dict(mapped)

    @staticmethod
    def _signal_outcome_row_to_outcome_record(row: aiosqlite.Row) -> OutcomeRecord:
        data = dict(row)
        result = str(data.get("result") or "")
        mapped = {
            "outcome_id": data.get("tracking_id"),
            "signal_id": data.get("tracking_id"),
            "symbol": data.get("symbol"),
            "max_profit_pct": data.get("max_profit_pct") or 0.0,
            "max_loss_pct": data.get("max_loss_pct") or 0.0,
            "mae": data.get("mae") or data.get("max_loss_pct") or 0.0,
            "mfe": data.get("mfe") or data.get("max_profit_pct") or 0.0,
            "hit_tp1": int(result in {"tp1_hit", "tp2_hit", "tp3_hit"}),
            "hit_tp2": int(result in {"tp2_hit", "tp3_hit"}),
            "hit_sl": int(result in {"stop_loss", "breakeven_stop", "trailing_stop"}),
            "result": result,
            "updated_at": data.get("closed_at") or data.get("created_at"),
            "closed_at": data.get("closed_at"),
            "pnl_24h": data.get("pnl_pct"),
        }
        return OutcomeRecord.from_dict(mapped)

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

    def _cooldown_cache_get(self, cooldown_key: str) -> datetime | None | object:
        loaded_at = self._cooldown_cache_mono.get(cooldown_key)
        if loaded_at is None:
            return _CACHE_MISS
        if (time.monotonic() - loaded_at) > self._cooldown_cache_ttl_s:
            self._cooldown_cache.pop(cooldown_key, None)
            self._cooldown_cache_mono.pop(cooldown_key, None)
            return _CACHE_MISS
        return self._cooldown_cache.get(cooldown_key)

    def _cooldown_cache_put(self, cooldown_key: str, sent_at: datetime | None) -> None:
        now = time.monotonic()
        if sent_at is None:
            self._cooldown_cache.pop(cooldown_key, None)
            self._cooldown_cache_mono[cooldown_key] = now
            return
        self._cooldown_cache[cooldown_key] = sent_at
        self._cooldown_cache_mono[cooldown_key] = now

    async def get_cooldown(self, cooldown_key: str) -> datetime | None:
        """Get last sent time for a cooldown key."""
        if not self._conn:
            msg = "Repository not initialized"
            raise RuntimeError(msg)

        cached = self._cooldown_cache_get(cooldown_key)
        if cached is not _CACHE_MISS:
            return cached if isinstance(cached, datetime) else None

        async with self._conn.execute(
            "SELECT last_sent_at FROM cooldowns WHERE cooldown_key = ?", (cooldown_key,)
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                parsed = datetime.fromisoformat(row["last_sent_at"])
                self._cooldown_cache_put(cooldown_key, parsed)
                return parsed
            self._cooldown_cache_put(cooldown_key, None)
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
        self._cooldown_cache_put(cooldown_key, sent_at.astimezone(UTC))

    async def read_market_cache(self, cache_key: str, *, max_age_s: float) -> str | None:
        if not self._conn:
            return None

        return await read_market_data_cache(self._conn, cache_key, max_age_s=max_age_s)

    async def write_market_cache(
        self,
        cache_key: str,
        payload_json: str,
        *,
        ttl_seconds: int = 3600,
    ) -> None:
        if not self._conn:
            return

        await write_market_data_cache(
            self._conn,
            cache_key,
            payload_json,
            ttl_seconds=ttl_seconds,
        )

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

    def setup_history_count(self, setup_id: str) -> int:
        """Sync outcome-window length for confluence prior calibration."""
        try:
            with sqlite3.connect(self._db_path) as conn:
                row = conn.execute(
                    "SELECT outcome_window FROM setup_scores WHERE setup_id = ?",
                    (setup_id,),
                ).fetchone()
        except DEFENSIVE_EXC:
            LOG.debug("setup_history_count read failed | setup_id=%s", setup_id, exc_info=True)
            return 0
        if row is None or not row[0]:
            return 0
        try:
            window = json.loads(row[0])
        except json.JSONDecodeError:
            return 0
        return len(window) if isinstance(window, list) else 0

    def setup_win_rate(self, setup_id: str) -> float | None:
        """Return win-rate from outcome_window, or None if insufficient data."""
        try:
            with sqlite3.connect(self._db_path) as conn:
                row = conn.execute(
                    "SELECT outcome_window FROM setup_scores WHERE setup_id = ?",
                    (setup_id,),
                ).fetchone()
        except DEFENSIVE_EXC:
            return None
        if row is None or not row[0]:
            return None
        try:
            window: list[Any] = json.loads(row[0])
        except (json.JSONDecodeError, TypeError):
            return None
        if not isinstance(window, list) or len(window) < 5:
            return None
        wins = 0
        total = 0
        for entry in window:
            if isinstance(entry, dict):
                outcome = entry.get("outcome", "")
                profitable = entry.get("was_profitable")
            else:
                outcome = str(entry)
                profitable = None
            _win_outcomes = {"tp1_hit", "tp2_hit", "tp3_hit", "partial_tp"}
            if outcome in _win_outcomes or profitable is True:
                wins += 1
                total += 1
            elif outcome == "stop_loss" or profitable is False:
                total += 1
        return wins / total if total >= 5 else None

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
            "entry_zone_touched_at",
            "entry_confirm_pending_at",
            "last_lifecycle_note",
            "trailing_stop",
            "entry_order_type",
            "entry_tf",
            "pattern_tf",
            "context_tfs",
        ]

        values = []
        for col in columns:
            val = signal_data.get(col)
            if col == "reasons" and isinstance(val, (list, tuple)):
                val = json.dumps(list(val))
            if col == "scale_weights" and isinstance(val, (list, tuple)):
                val = json.dumps(list(val))
            if col == "context_tfs" and isinstance(val, (list, tuple)):
                val = json.dumps(list(val))
            values.append(val)

        placeholders = ", ".join(["?"] * len(columns))
        updates = ", ".join([f"{col} = excluded.{col}" for col in columns if col != "tracking_id"])

        tracking_id = str(signal_data.get("tracking_id") or "")
        # fix-20260604: reject cross-signal tracking_id reuse (upsert is for lifecycle updates only)
        if tracking_id:
            async with self._conn.execute(
                "SELECT symbol, setup_id FROM active_signals WHERE tracking_id = ?",
                (tracking_id,),
            ) as cursor:
                existing = await cursor.fetchone()
            if existing is not None:
                old_symbol = str(existing["symbol"] or "")
                old_setup = str(existing["setup_id"] or "")
                new_symbol = str(signal_data.get("symbol") or "")
                new_setup = str(signal_data.get("setup_id") or "")
                if old_symbol != new_symbol or old_setup != new_setup:
                    msg = (
                        f"tracking_id collision | id={tracking_id} "
                        f"existing={old_symbol}/{old_setup} new={new_symbol}/{new_setup}"
                    )
                    LOG.error(msg)
                    raise ValueError(msg)

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
                    if data.get("context_tfs"):
                        try:
                            parsed = json.loads(data["context_tfs"])
                            data["context_tfs"] = (
                                tuple(str(tf) for tf in parsed) if isinstance(parsed, list) else ()
                            )
                        except (json.JSONDecodeError, TypeError, ValueError):
                            data["context_tfs"] = ()
                    result.append(data)
                return result
        except (aiosqlite.Error, sqlite3.Error):
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

    async def cleanup_active_signals_on_startup(self) -> int:
        """Expire all pending/active signals at startup (V.33).

        After a crash, ``active_signals`` may contain stale entries that block
        new signals via cooldown. This closes all non-closed signals immediately.
        """
        if not self._conn:
            msg = "Repository not initialized"
            raise RuntimeError(msg)
        now = datetime.now(UTC)
        cursor = await self._conn.execute(
            """
            UPDATE active_signals
            SET status = 'closed',
                close_reason = 'startup_cleanup',
                close_price = COALESCE(last_price, activation_price, entry_mid, close_price),
                closed_at = ?
            WHERE status IN ('pending', 'active')
            """,
            (now.isoformat(),),
        )
        changed = int(cursor.rowcount or 0)
        await cursor.close()
        await self._conn.commit()
        if changed:
            LOG.info("active_signals_cleaned_on_startup | count=%d", changed)
        return changed

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
                close_reason = CASE
                    WHEN activated_at IS NOT NULL AND tp1_hit_at IS NOT NULL THEN 'tp1_hit'
                    ELSE 'expired'
                END,
                close_price = CASE
                    WHEN activated_at IS NOT NULL AND tp1_hit_at IS NOT NULL
                        THEN COALESCE(
                            tp1_price, take_profit_1, last_price, activation_price, entry_mid
                        )
                    ELSE COALESCE(last_price, activation_price, entry_mid, close_price)
                END,
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

    def get_open_signal_counts_sync(self) -> dict[str, int]:
        """Sync read of pending/active counts for dashboard hot-path polls."""
        try:
            with sqlite3.connect(self._db_path, timeout=5.0) as conn:
                cursor = conn.execute(
                    "SELECT status, COUNT(*) FROM active_signals "
                    "WHERE status IN ('pending','active') GROUP BY status"
                )
                rows = cursor.fetchall()
            counts = {str(status): int(count) for status, count in rows}
            pending = counts.get("pending", 0)
            active = counts.get("active", 0)
            return {"pending": pending, "active": active, "open": pending + active}
        except (OSError, sqlite3.Error):
            return {"pending": 0, "active": 0, "open": 0}

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
                entry_tf, pattern_tf, context_tfs,
                created_at, activated_at, closed_at, entry_price, exit_price, result,
                pnl_pct, pnl_r_multiple, max_profit_pct, max_loss_pct, mae, mfe,
                time_to_entry_min, time_to_exit_min, features, was_profitable,
                llm_was_correct, setup_quality
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(tracking_id) DO UPDATE SET
                signal_id = excluded.signal_id,
                tracking_ref = excluded.tracking_ref,
                symbol = excluded.symbol,
                setup_id = excluded.setup_id,
                direction = excluded.direction,
                timeframe = excluded.timeframe,
                entry_tf = excluded.entry_tf,
                pattern_tf = excluded.pattern_tf,
                context_tfs = excluded.context_tfs,
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
                    item.get("entry_tf") or item.get("entry_tf_used") or item["timeframe"],
                    item.get("pattern_tf", ""),
                    json.dumps(
                        list(item.get("context_tfs") or [])
                        if not isinstance(item.get("context_tfs"), str)
                        else item.get("context_tfs")
                    ),
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
