"""Quick schema inspection helper."""

import sqlite3
from pathlib import Path

db = Path(__file__).resolve().parents[1] / "data" / "bot" / "bot.db"
conn = sqlite3.connect(db)
tables = [
    r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY 1")
]
print("tables:", tables)
print("schema_version:", list(conn.execute("SELECT version, description FROM schema_version")))
print("trader_diary exists:", "trader_diary" in tables)
conn.close()
