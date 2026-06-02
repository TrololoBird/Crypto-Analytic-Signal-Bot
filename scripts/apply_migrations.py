"""Apply pending DB migrations."""

from __future__ import annotations

import asyncio
from pathlib import Path

import aiosqlite

from bot.migrations import migrate_db


async def main() -> None:
    db_path = Path(__file__).resolve().parents[1] / "data" / "bot" / "bot.db"
    conn = await aiosqlite.connect(str(db_path))
    try:
        applied = await migrate_db(conn)
        print(f"applied migrations: {applied}")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
