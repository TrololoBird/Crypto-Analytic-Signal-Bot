"""Analytics and stats CRUD mixin for MemoryRepository."""

from __future__ import annotations

import json
import logging
import math
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import aiosqlite

from ..outcomes import aggregate_setup_stats

# fix-20260604: LOG for outcome_window decode failures (Phase F extraction)
LOG = logging.getLogger("bot.persistence.repository.analytics")


async def fetch_setup_stats_rows(
    conn: aiosqlite.Connection,
    *,
    setup_id: str | None = None,
    last_days: int | None = 90,
    since: datetime | str | None = None,
) -> list[dict[str, Any]]:
    """Return raw signal_outcomes rows for setup stats aggregation."""
    query = """
        SELECT setup_id, result, was_profitable, pnl_r_multiple, pnl_pct, activated_at
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
        since_dt = datetime.now(UTC) - timedelta(days=last_days)
        query += " AND COALESCE(closed_at, created_at) >= ?"
        params.append(since_dt.isoformat())

    async with conn.execute(query, params) as cursor:
        rows = await cursor.fetchall()

    return [
        {
            "setup_id": row["setup_id"],
            "result": row["result"],
            "was_profitable": row["was_profitable"],
            "pnl_r_multiple": row["pnl_r_multiple"],
            "pnl_pct": row["pnl_pct"],
            "activated_at": row["activated_at"],
        }
        for row in rows
    ]


async def fetch_signal_outcome_rows(
    conn: aiosqlite.Connection,
    *,
    setup_id: str | None = None,
    symbol: str | None = None,
    result: str | None = None,
    last_days: int | None = 30,
    since: datetime | str | None = None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """Return parsed signal_outcomes rows."""
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
        since_dt = datetime.now(UTC) - timedelta(days=last_days)
        query += " AND COALESCE(closed_at, created_at) >= ?"
        params.append(since_dt.isoformat())

    query += " ORDER BY COALESCE(closed_at, created_at) DESC"
    if limit is not None:
        query += " LIMIT ?"
        params.append(limit)

    async with conn.execute(query, params) as cursor:
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


class AnalyticsMixin:
    """Outcome/setup/symbol stats operations (extracted from memory.py)."""

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
            # fix-20260604: use mixin staticmethod (MemoryRepository not imported here)
            r_value = AnalyticsMixin._setup_outcome_r_multiple(item)
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

        payload = await fetch_setup_stats_rows(
            self._conn,
            setup_id=setup_id,
            last_days=last_days,
            since=since,
        )
        return aggregate_setup_stats(payload)

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

        return await fetch_signal_outcome_rows(
            self._conn,
            setup_id=setup_id,
            symbol=symbol,
            result=result,
            last_days=last_days,
            since=since,
            limit=limit,
        )

    async def get_symbol_sl_counts(self, *, last_days: int = 7) -> dict[str, int]:
        """Count stop_loss outcomes per symbol over a recent window."""
        if not self._conn:
            msg = "Repository not initialized"
            raise RuntimeError(msg)
        since = datetime.now(UTC) - timedelta(days=max(1, int(last_days)))
        query = """
            SELECT symbol, COUNT(*) AS sl_count
            FROM signal_outcomes
            WHERE result = 'stop_loss'
              AND COALESCE(closed_at, created_at) >= ?
            GROUP BY symbol
        """
        async with self._conn.execute(query, (since.isoformat(),)) as cursor:
            rows = await cursor.fetchall()
        return {str(row["symbol"]).upper(): int(row["sl_count"] or 0) for row in rows}

    async def get_symbol_sl_event_ages(self, *, last_days: int = 7) -> dict[str, list[float]]:
        """Return SL event ages in days per symbol for outcome derank decay."""
        if not self._conn:
            msg = "Repository not initialized"
            raise RuntimeError(msg)
        since = datetime.now(UTC) - timedelta(days=max(1, int(last_days)))
        query = """
            SELECT symbol, COALESCE(closed_at, created_at) AS closed_at
            FROM signal_outcomes
            WHERE result = 'stop_loss'
              AND COALESCE(closed_at, created_at) >= ?
        """
        async with self._conn.execute(query, (since.isoformat(),)) as cursor:
            rows = await cursor.fetchall()
        now = datetime.now(UTC)
        ages: dict[str, list[float]] = {}
        for row in rows:
            symbol = str(row["symbol"]).upper()
            closed_raw = row["closed_at"]
            try:
                closed_at = datetime.fromisoformat(str(closed_raw))
            except (TypeError, ValueError):
                continue
            if closed_at.tzinfo is None:
                closed_at = closed_at.replace(tzinfo=UTC)
            age_days = max(0.0, (now - closed_at).total_seconds() / 86_400.0)
            ages.setdefault(symbol, []).append(age_days)
        return ages

    async def merge_outcome_features(
        self,
        tracking_id: str,
        patch: dict[str, Any],
    ) -> None:
        """Merge keys into the JSON features blob for a persisted outcome."""
        if not self._conn or not patch:
            msg = "Repository not initialized" if not self._conn else ""
            if not self._conn:
                raise RuntimeError(msg)
            return
        async with self._conn.execute(
            "SELECT features FROM signal_outcomes WHERE tracking_id = ?",
            (str(tracking_id),),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return
        raw = row["features"]
        try:
            features = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            features = {}
        if not isinstance(features, dict):
            features = {}
        features.update(patch)
        await self._conn.execute(
            "UPDATE signal_outcomes SET features = ? WHERE tracking_id = ?",
            (json.dumps(features, separators=(",", ":"), default=str), str(tracking_id)),
        )
        await self._conn.commit()

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

    async def win_rate_by_strategy(
        self,
        *,
        setup_id: str | None = None,
        last_days: int = 30,
    ) -> dict[str, dict[str, float]]:
        """Return win-rate, total trades, avg RR per strategy (V.28).

        Returns dict keyed by setup_id with ``win_rate``, ``total``, ``wins``,
        ``avg_r_multiple``.
        """
        if not self._conn:
            msg = "Repository not initialized"
            raise RuntimeError(msg)
        since = (datetime.now(UTC) - timedelta(days=last_days)).isoformat()
        query = """
            SELECT
                setup_id,
                COUNT(*) AS total,
                SUM(CASE WHEN result IN ('tp1_hit', 'tp2_hit', 'breakeven_stop')
                    THEN 1 ELSE 0 END) AS wins,
                AVG(pnl_r_multiple) AS avg_r_multiple
            FROM signal_outcomes
            WHERE COALESCE(closed_at, created_at) >= ?
              AND result NOT IN ('expired_pending', 'expired', 'unactivated_close', 'superseded')
        """
        params: list[Any] = [since]
        if setup_id:
            query += " AND setup_id = ?"
            params.append(setup_id)
        query += " GROUP BY setup_id"
        result: dict[str, dict[str, float]] = {}
        async with self._conn.execute(query, params) as cursor:
            rows = await cursor.fetchall()
        for row in rows:
            sid = str(row["setup_id"])
            total = max(float(row["total"] or 0), 0.0)
            wins = max(float(row["wins"] or 0), 0.0)
            result[sid] = {
                "win_rate": round(wins / total, 4) if total > 0 else 0.0,
                "total": int(total),
                "wins": int(wins),
                "avg_r_multiple": round(float(row["avg_r_multiple"] or 0.0), 4),
            }
        return result

    async def monthly_setup_metrics(
        self,
        *,
        setup_id: str | None = None,
        months: int = 3,
    ) -> list[dict[str, Any]]:
        if not self._conn:
            msg = "Repository not initialized"
            raise RuntimeError(msg)
        since = (datetime.now(UTC) - timedelta(days=months * 30)).isoformat()
        query = """
            SELECT
                strftime('%Y-%m', COALESCE(closed_at, created_at)) AS month,
                setup_id,
                COUNT(*) AS total,
                SUM(CASE WHEN was_profitable = 1 THEN 1 ELSE 0 END) AS wins,
                AVG(pnl_r_multiple) AS avg_r_multiple
            FROM signal_outcomes
            WHERE COALESCE(closed_at, created_at) >= ?
        """
        params: list[Any] = [since]
        if setup_id:
            query += " AND setup_id = ?"
            params.append(setup_id)
        query += " GROUP BY month, setup_id ORDER BY month DESC, setup_id"
        result: list[dict[str, Any]] = []
        async with self._conn.execute(query, params) as cursor:
            rows = await cursor.fetchall()
        for row in rows:
            total = int(row["total"] or 0)
            wins = int(row["wins"] or 0)
            win_rate = round(wins / total, 4) if total > 0 else 0.0
            result.append(
                {
                    "month": str(row["month"]),
                    "setup_id": str(row["setup_id"]),
                    "total": total,
                    "wins": wins,
                    "win_rate": win_rate,
                    "avg_r_multiple": round(float(row["avg_r_multiple"] or 0.0), 4),
                }
            )
        return result

    async def export_signal_outcomes_csv(
        self,
        *,
        setup_id: str | None = None,
        last_days: int = 90,
        since: datetime | str | None = None,
    ) -> str:
        if not self._conn:
            msg = "Repository not initialized"
            raise RuntimeError(msg)
        query = """
            SELECT tracking_id, setup_id, symbol, direction, result,
                   was_profitable, pnl_r_multiple, pnl_pct,
                   activated_at, closed_at, created_at
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
            since_dt = datetime.now(UTC) - timedelta(days=last_days)
            query += " AND COALESCE(closed_at, created_at) >= ?"
            params.append(since_dt.isoformat())
        query += " ORDER BY COALESCE(closed_at, created_at) DESC"
        header = (
            "tracking_id,setup_id,symbol,direction,result,was_profitable,"
            "pnl_r_multiple,pnl_pct,activated_at,closed_at,created_at"
        )
        async with self._conn.execute(query, params) as cursor:
            rows = await cursor.fetchall()
        lines = [header]
        for row in rows:
            cols = [
                str(row["tracking_id"] or ""),
                str(row["setup_id"] or ""),
                str(row["symbol"] or ""),
                str(row["direction"] or ""),
                str(row["result"] or ""),
                str(int(bool(row["was_profitable"]))),
                str(row["pnl_r_multiple"] or ""),
                str(row["pnl_pct"] or ""),
                str(row["activated_at"] or ""),
                str(row["closed_at"] or ""),
                str(row["created_at"] or ""),
            ]
            lines.append(",".join(cols))
        return "\n".join(lines)
