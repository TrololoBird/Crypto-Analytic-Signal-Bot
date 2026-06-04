"""Signal outcome read queries extracted from MemoryRepository."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import aiosqlite


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
