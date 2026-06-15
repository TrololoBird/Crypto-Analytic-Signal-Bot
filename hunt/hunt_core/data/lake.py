"""Batch tick JSONL + feature parquet lake + tracker flush off hot path (P9)."""
from __future__ import annotations

import json
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

import polars as pl

from hunt_core.paths import LAKE_PARQUET, TICK_JSONL


class LakeDataError(RuntimeError):
    """Feature lake read/write failure."""


_tick_lines: list[str] = []
_tracker_flush: tuple[dict[str, Any], Path] | None = None
_cooldown_flush: tuple[dict[str, str], Path] | None = None


def buffer_tick_rows(rows: list[dict[str, Any]]) -> None:
    for row in rows:
        _tick_lines.append(json.dumps(row, default=str))


def flush_tick_buffer() -> int:
    if not _tick_lines:
        return 0
    TICK_JSONL.parent.mkdir(parents=True, exist_ok=True)
    with TICK_JSONL.open("a", encoding="utf-8") as fh:
        for line in _tick_lines:
            fh.write(line + "\n")
    n = len(_tick_lines)
    _tick_lines.clear()
    return n


def buffer_tracker_state(state: dict[str, Any], path: Path | None = None) -> None:
    global _tracker_flush
    from hunt_core.track.tracker import STATE_PATH

    _tracker_flush = (state, path or STATE_PATH)


def buffer_cooldown_state(state: dict[str, str], path: Path) -> None:
    global _cooldown_flush
    _cooldown_flush = (state, path)


def flush_tracker_state() -> bool:
    global _tracker_flush
    if _tracker_flush is None:
        return False
    state, path = _tracker_flush
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2, default=str), encoding="utf-8")
    _tracker_flush = None
    return True


def flush_cooldown_state() -> bool:
    global _cooldown_flush
    if _cooldown_flush is None:
        return False
    state, path = _cooldown_flush
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2), encoding="utf-8")
    _cooldown_flush = None
    return True


def flush_lake() -> None:
    flush_tick_buffer()
    flush_tracker_state()
    flush_cooldown_state()


def _parquet_path(symbol: str, tf: str) -> Path:
    sym = str(symbol or "").strip().upper()
    return LAKE_PARQUET / sym / f"{tf}.parquet"


class FeatureLakeWriter:
    """Buffered parquet feature writer — flush on close."""

    def __init__(self) -> None:
        self._buf: dict[tuple[str, str], list[dict[str, Any]]] = {}
        self._lock = threading.Lock()

    def enqueue(self, symbol: str, ts: str, tf: str, payload: dict[str, Any]) -> None:
        row = {"symbol": symbol, "ts": ts, "tf": tf, **payload}
        key = (str(symbol).upper(), str(tf))
        with self._lock:
            self._buf.setdefault(key, []).append(row)

    def close(self) -> None:
        with self._lock:
            pending = dict(self._buf)
            self._buf.clear()
        for (symbol, tf), rows in pending.items():
            if not rows:
                continue
            path = _parquet_path(symbol, tf)
            path.parent.mkdir(parents=True, exist_ok=True)
            new_df = pl.DataFrame(rows)
            if path.exists():
                try:
                    old = pl.read_parquet(path)
                    new_df = pl.concat([old, new_df], how="diagonal_relaxed")
                except DEFENSIVE_EXC:
                    pass
            new_df.write_parquet(path)


DEFENSIVE_EXC = (OSError, ValueError, pl.exceptions.PolarsError)


def read_features(
    symbol: str,
    start_ts: str,
    end_ts: str,
    *,
    tf: str = "15m",
) -> pl.DataFrame:
    path = _parquet_path(symbol, tf)
    if not path.exists():
        raise LakeDataError(f"no lake parquet for {symbol} {tf}")
    df = pl.read_parquet(path)
    if df.is_empty() or "ts" not in df.columns:
        return df
    return df.filter((pl.col("ts") >= start_ts) & (pl.col("ts") <= end_ts))


def append_feature_row(symbol: str, ts: str, tf: str, payload: dict[str, Any]) -> None:
    w = FeatureLakeWriter()
    w.enqueue(symbol, ts, tf, payload)
    w.close()


def get_feature_lake_writer() -> FeatureLakeWriter:
    return FeatureLakeWriter()


def serialize_tick_row(row: dict[str, Any]) -> str:
    return json.dumps(row, default=str)


def append_tick_rows(rows: list[dict[str, Any]]) -> int:
    buffer_tick_rows(rows)
    return flush_tick_buffer()


def query_features(symbol: str, *, tf: str = "15m", limit: int = 500) -> pl.DataFrame:
    path = _parquet_path(symbol, tf)
    if not path.exists():
        return pl.DataFrame()
    df = pl.read_parquet(path)
    if limit > 0 and df.height > limit:
        return df.tail(limit)
    return df


def query_baseline_stats(symbol: str, *, tf: str = "15m") -> dict[str, Any]:
    df = query_features(symbol, tf=tf, limit=0)
    if df.is_empty():
        return {"symbol": symbol, "tf": tf, "rows": 0}
    return {"symbol": symbol, "tf": tf, "rows": df.height}


def import_ticks_to_lake(_path: Path) -> int:
    return 0


class LakeStore:
    """Compat placeholder for lake sqlite backend."""

    def __init__(self, _path: Path | None = None) -> None:
        pass


__all__ = [
    "FeatureLakeWriter",
    "LakeDataError",
    "LakeStore",
    "append_feature_row",
    "append_tick_rows",
    "buffer_cooldown_state",
    "buffer_tick_rows",
    "buffer_tracker_state",
    "flush_cooldown_state",
    "flush_lake",
    "flush_tick_buffer",
    "flush_tracker_state",
    "get_feature_lake_writer",
    "import_ticks_to_lake",
    "query_baseline_stats",
    "query_features",
    "read_features",
    "serialize_tick_row",
]
