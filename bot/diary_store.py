"""Trader diary persistent store backed by SQLite.

Links trades to bot signals for post-decision analytics.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiosqlite

from .migrations import migrate_db

UTC = timezone.utc
LOG = logging.getLogger("bot.diary_store")

TRADE_COLS = [
    "id", "linked_signal_id", "decision",
    "entry_price", "entry_time",
    "size_amount", "leverage", "risk_percent",
    "sl_price", "sl_source",
    "tp_prices", "tp_hit_level",
    "exit_price", "exit_time", "exit_reason",
    "pnl_percent", "pnl_usd",
    "mood", "tags", "notes", "screenshot_path",
    "bot_signal_snapshot",
    "created_at", "closed_at", "updated_at",
]


def _row_to_dict(row: aiosqlite.Row) -> dict[str, Any]:
    d = dict(row)
    if d.get("tp_prices") and isinstance(d["tp_prices"], str):
        d["tp_prices"] = json.loads(d["tp_prices"])
    if d.get("tags") and isinstance(d["tags"], str):
        d["tags"] = json.loads(d["tags"])
    if d.get("bot_signal_snapshot") and isinstance(d["bot_signal_snapshot"], str):
        d["bot_signal_snapshot"] = json.loads(d["bot_signal_snapshot"])
    return d


class DiaryStore:
    """CRUD for trader_diary table, sharing the bot's SQLite database."""

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = Path(db_path)
        self._conn: aiosqlite.Connection | None = None

    async def initialize(self) -> None:
        self._conn = await aiosqlite.connect(str(self._db_path))
        self._conn.row_factory = aiosqlite.Row
        await migrate_db(self._conn)
        LOG.info("diary store initialized at %s", self._db_path)

    def _conn_or_raise(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("DiaryStore not initialized")
        return self._conn

    async def create_trade(self, trade: dict[str, Any]) -> dict[str, Any]:
        conn = self._conn_or_raise()
        trade_id = str(uuid.uuid4())
        now = datetime.now(UTC).isoformat()
        row = {
            "id": trade_id,
            "linked_signal_id": trade.get("linked_signal_id"),
            "decision": trade.get("decision", "took_signal"),
            "entry_price": trade.get("entry_price"),
            "entry_time": trade.get("entry_time") or now,
            "size_amount": trade.get("size_amount"),
            "leverage": trade.get("leverage"),
            "risk_percent": trade.get("risk_percent"),
            "sl_price": trade.get("sl_price"),
            "sl_source": trade.get("sl_source", "bot"),
            "tp_prices": json.dumps(trade.get("tp_prices") or []),
            "tp_hit_level": trade.get("tp_hit_level"),
            "exit_price": trade.get("exit_price"),
            "exit_time": trade.get("exit_time"),
            "exit_reason": trade.get("exit_reason"),
            "pnl_percent": trade.get("pnl_percent"),
            "pnl_usd": trade.get("pnl_usd"),
            "mood": trade.get("mood"),
            "tags": json.dumps(trade.get("tags") or []),
            "notes": trade.get("notes"),
            "screenshot_path": trade.get("screenshot_path"),
            "bot_signal_snapshot": json.dumps(trade.get("bot_signal_snapshot") or {}),
            "created_at": now,
            "updated_at": now,
        }
        placeholders = ", ".join(f":{k}" for k in row)
        columns = ", ".join(row.keys())
        await conn.execute(
            f"INSERT INTO trader_diary ({columns}) VALUES ({placeholders})",
            row,
        )
        await conn.commit()
        LOG.info("diary trade created | id=%s", trade_id)
        return await self.get_trade(trade_id)

    async def get_trade(self, trade_id: str) -> dict[str, Any] | None:
        conn = self._conn_or_raise()
        async with conn.execute(
            "SELECT * FROM trader_diary WHERE id = ?", (trade_id,)
        ) as cursor:
            row = await cursor.fetchone()
        return _row_to_dict(row) if row else None

    async def update_trade(
        self, trade_id: str, updates: dict[str, Any]
    ) -> dict[str, Any] | None:
        conn = self._conn_or_raise()
        now = datetime.now(UTC).isoformat()
        safe_updates = {
            k: v for k, v in updates.items()
            if k in TRADE_COLS and k != "id"
        }
        safe_updates["updated_at"] = now
        if "tp_prices" in safe_updates and isinstance(safe_updates["tp_prices"], list):
            safe_updates["tp_prices"] = json.dumps(safe_updates["tp_prices"])
        if "tags" in safe_updates and isinstance(safe_updates["tags"], list):
            safe_updates["tags"] = json.dumps(safe_updates["tags"])
        if "bot_signal_snapshot" in safe_updates and isinstance(
            safe_updates["bot_signal_snapshot"], dict
        ):
            safe_updates["bot_signal_snapshot"] = json.dumps(
                safe_updates["bot_signal_snapshot"]
            )
        set_clause = ", ".join(f"{k} = :{k}" for k in safe_updates)
        safe_updates["id"] = trade_id
        await conn.execute(
            f"UPDATE trader_diary SET {set_clause} WHERE id = :id",
            safe_updates,
        )
        await conn.commit()
        return await self.get_trade(trade_id)

    async def close_trade(
        self,
        trade_id: str,
        exit_price: float,
        exit_time: str,
        exit_reason: str,
        pnl_percent: float | None = None,
        pnl_usd: float | None = None,
        tp_hit_level: int | None = None,
        mood: str | None = None,
        notes: str | None = None,
    ) -> dict[str, Any] | None:
        return await self.update_trade(trade_id, {
            "exit_price": exit_price,
            "exit_time": exit_time,
            "exit_reason": exit_reason,
            "pnl_percent": pnl_percent,
            "pnl_usd": pnl_usd,
            "tp_hit_level": tp_hit_level,
            "mood": mood,
            "notes": notes,
            "closed_at": exit_time,
        })

    async def list_trades(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
        status: str | None = None,
        symbol: str | None = None,
        decision: str | None = None,
        from_date: str | None = None,
        to_date: str | None = None,
    ) -> list[dict[str, Any]]:
        conn = self._conn_or_raise()
        conditions: list[str] = []
        params: list[Any] = []
        if status == "open":
            conditions.append("exit_price IS NULL")
        elif status == "closed":
            conditions.append("exit_price IS NOT NULL")
        if symbol:
            conditions.append("bot_signal_snapshot LIKE ?")
            params.append(f"%{symbol}%")
        if decision:
            conditions.append("decision = ?")
            params.append(decision)
        if from_date:
            conditions.append("entry_time >= ?")
            params.append(from_date)
        if to_date:
            conditions.append("entry_time <= ?")
            params.append(to_date)
        where = " AND ".join(conditions) if conditions else "1"
        query = (
            f"SELECT * FROM trader_diary WHERE {where} "
            f"ORDER BY entry_time DESC LIMIT ? OFFSET ?"
        )
        params.extend([max(1, int(limit)), max(0, int(offset))])
        async with conn.execute(query, params) as cursor:
            rows = await cursor.fetchall()
        return [_row_to_dict(r) for r in rows]

    async def get_analytics(self, days: int = 30) -> dict[str, Any]:
        conn = self._conn_or_raise()
        params: list[Any] = [days]

        async with conn.execute(
            "SELECT COUNT(*) as total, "
            "SUM(CASE WHEN exit_reason IN ('tp1','tp2','tp3') THEN 1 ELSE 0 END) as wins, "
            "SUM(CASE WHEN exit_reason = 'sl' THEN 1 ELSE 0 END) as losses, "
            "AVG(pnl_percent) as avg_pnl_pct, "
            "AVG(pnl_usd) as avg_pnl_usd "
            "FROM trader_diary "
            "WHERE exit_price IS NOT NULL "
            "AND entry_time >= datetime('now', '-' || ? || ' days')",
            params,
        ) as cursor:
            summary_row = await cursor.fetchone()

        async with conn.execute(
            "SELECT decision, COUNT(*) as count "
            "FROM trader_diary "
            "WHERE entry_time >= datetime('now', '-' || ? || ' days') "
            "GROUP BY decision ORDER BY count DESC",
            params,
        ) as cursor:
            decision_rows = await cursor.fetchall()

        async with conn.execute(
            "SELECT exit_reason, COUNT(*) as count "
            "FROM trader_diary "
            "WHERE exit_price IS NOT NULL "
            "AND entry_time >= datetime('now', '-' || ? || ' days') "
            "GROUP BY exit_reason ORDER BY count DESC",
            params,
        ) as cursor:
            outcome_rows = await cursor.fetchall()

        async with conn.execute(
            "SELECT mood, COUNT(*) as count, AVG(pnl_percent) as avg_pnl "
            "FROM trader_diary "
            "WHERE mood IS NOT NULL AND exit_price IS NOT NULL "
            "AND entry_time >= datetime('now', '-' || ? || ' days') "
            "GROUP BY mood ORDER BY count DESC",
            params,
        ) as cursor:
            mood_rows = await cursor.fetchall()

        async with conn.execute(
            "SELECT DATE(entry_time) as day, "
            "SUM(CASE WHEN exit_reason IN ('tp1','tp2','tp3') THEN 1 ELSE 0 END) as wins, "
            "SUM(CASE WHEN exit_reason = 'sl' THEN 1 ELSE 0 END) as losses, "
            "SUM(pnl_usd) as day_pnl "
            "FROM trader_diary "
            "WHERE exit_price IS NOT NULL "
            "AND entry_time >= datetime('now', '-' || ? || ' days') "
            "GROUP BY DATE(entry_time) ORDER BY day",
            params,
        ) as cursor:
            calendar_rows = await cursor.fetchall()

        total = summary_row["total"] if summary_row else 0
        wins = summary_row["wins"] if summary_row else 0
        losses = summary_row["losses"] if summary_row else 0
        closed = wins + losses
        return {
            "days": days,
            "summary": {
                "total_trades": total,
                "closed_trades": closed,
                "win_rate": round(wins / max(closed, 1), 4),
                "avg_pnl_percent": round(summary_row["avg_pnl_pct"] or 0.0, 2),
                "avg_pnl_usd": round(summary_row["avg_pnl_usd"] or 0.0, 2),
            },
            "by_decision": [
                dict(r) for r in decision_rows
            ],
            "by_outcome": [
                dict(r) for r in outcome_rows
            ],
            "by_mood": [
                dict(r) for r in mood_rows
            ],
            "calendar": [
                {
                    "day": r["day"],
                    "wins": r["wins"],
                    "losses": r["losses"],
                    "pnl_usd": r["day_pnl"],
                }
                for r in calendar_rows
            ],
        }

    async def delete_trade(self, trade_id: str) -> bool:
        conn = self._conn_or_raise()
        await conn.execute("DELETE FROM trader_diary WHERE id = ?", (trade_id,))
        await conn.commit()
        return True
