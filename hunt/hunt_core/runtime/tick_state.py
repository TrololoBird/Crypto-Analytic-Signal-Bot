"""Last tick row store — read-only probes, no REST re-fetch."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from hunt_core.paths import TICK_JSONL


class LastTickStore:
    """In-memory mirror of the latest assembled tick row per symbol."""

    def __init__(self) -> None:
        self._rows: dict[str, dict[str, Any]] = {}

    def put(self, symbol: str, row: dict[str, Any]) -> None:
        sym = str(symbol or "").upper()
        if not sym or not isinstance(row, dict):
            return
        slim = {k: v for k, v in row.items() if k != "_prepared"}
        self._rows[sym] = slim

    def put_many(self, rows: list[dict[str, Any]]) -> None:
        for row in rows:
            sym = str(row.get("symbol") or "").upper()
            if sym:
                self.put(sym, row)

    def get(self, symbol: str) -> dict[str, Any] | None:
        sym = str(symbol or "").upper()
        row = self._rows.get(sym)
        return dict(row) if isinstance(row, dict) else None

    def tail_jsonl(self, symbol: str, *, path: Path = TICK_JSONL) -> dict[str, Any] | None:
        sym = str(symbol or "").upper()
        if not sym or not path.exists():
            return None
        try:
            with path.open("rb") as fh:
                fh.seek(0, 2)
                size = fh.tell()
                chunk = min(size, 256_000)
                fh.seek(max(0, size - chunk))
                tail = fh.read().decode("utf-8", errors="replace")
        except OSError:
            return None
        for line in reversed(tail.splitlines()):
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict) and str(row.get("symbol") or "").upper() == sym:
                return row
        return None

    def resolve(self, symbol: str, *, jsonl_fallback: bool = True) -> dict[str, Any] | None:
        row = self.get(symbol)
        if row is not None:
            return row
        if jsonl_fallback:
            return self.tail_jsonl(symbol)
        return None


_STORE: LastTickStore | None = None


def last_tick_store() -> LastTickStore:
    global _STORE
    if _STORE is None:
        _STORE = LastTickStore()
    return _STORE


__all__ = ["LastTickStore", "last_tick_store"]
