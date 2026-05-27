from __future__ import annotations

import csv
import hashlib
import json
import logging
import re
import shutil
import threading
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import polars as pl


UTC = timezone.utc
LOG = logging.getLogger("bot.telemetry")
_CSV_LOCKS_GUARD = threading.Lock()
_CSV_LOCKS: dict[str, threading.Lock] = {}
_CSV_COMPACT_CALLS: dict[str, int] = {}
_CSV_LAST_TIME: dict[str, str] = {}


def symbol_storage_dirname(symbol: str) -> str:
    raw = str(symbol or "").strip()
    if not raw:
        return "symbol"
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", raw).strip("._")
    if not safe or safe in {".", ".."}:
        safe = "symbol"
    if safe == raw and "/" not in raw and "\\" not in raw:
        return safe
    digest = hashlib.sha1(raw.encode("utf-8"), usedforsecurity=False).hexdigest()[:10]
    return f"{safe}__{digest}"


def rotate_file_if_needed(path: Path, max_size_mb: int) -> None:
    if max_size_mb <= 0 or not path.exists():
        return
    max_bytes = max_size_mb * 1024 * 1024
    if path.stat().st_size <= max_bytes:
        return
    stamp = date.today().isoformat()
    archive = path.with_name(f"{path.stem}.{stamp}{path.suffix}")
    counter = 1
    while archive.exists():
        archive = path.with_name(f"{path.stem}.{stamp}.{counter}{path.suffix}")
        counter += 1
    archive.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(path), str(archive))


class TelemetryStore:
    def __init__(
        self, base_dir: Path, run_id: str | None = None, rotation_max_mb: int = 50
    ) -> None:
        self.root_dir = base_dir
        self.run_id = run_id
        self.started_at = datetime.now(UTC)
        self.rotation_max_mb = max(1, int(rotation_max_mb))
        self.base_dir = base_dir / "runs" / run_id if run_id else base_dir
        self.analysis_dir = self.base_dir / "analysis"
        self.raw_dir = self.base_dir / "raw"
        self.features_dir = self.base_dir / "features"
        self.replay_dir = self.base_dir / "replay"
        self.market_dir = self.base_dir / "market_history"
        self.market_dir.mkdir(parents=True, exist_ok=True)
        self.analysis_dir.mkdir(parents=True, exist_ok=True)
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        self.features_dir.mkdir(parents=True, exist_ok=True)
        self.replay_dir.mkdir(parents=True, exist_ok=True)
        self._candle_append_counts: dict[str, int] = {}
        self._candle_last_time: dict[str, str] = {}
        if run_id:
            metadata_path = self.base_dir / "run_metadata.json"
            if not metadata_path.exists():
                metadata_path.write_text(
                    json.dumps(
                        {
                            "run_id": run_id,
                            "started_at": self.started_at.isoformat(),
                            "schema_version": 2,
                        },
                        indent=2,
                        ensure_ascii=True,
                    ),
                    encoding="utf-8",
                )

    def append_jsonl(self, relative_name: str, row: dict[str, Any]) -> None:
        path = self.analysis_dir / relative_name
        self._append_jsonl_path(path, row)

    def append_raw_jsonl(self, relative_name: str, row: dict[str, Any]) -> None:
        path = self.raw_dir / relative_name
        self._append_jsonl_path(path, row)

    def append_feature_jsonl(self, relative_name: str, row: dict[str, Any]) -> None:
        path = self.features_dir / relative_name
        self._append_jsonl_path(path, row)

    def append_replay_jsonl(self, relative_name: str, row: dict[str, Any]) -> None:
        path = self.replay_dir / relative_name
        self._append_jsonl_path(path, row)

    def append_symbol_jsonl(
        self, bucket: str, symbol: str, relative_name: str, row: dict[str, Any]
    ) -> None:
        if bucket == "analysis":
            base_dir = self.analysis_dir
        elif bucket == "raw":
            base_dir = self.raw_dir
        elif bucket == "features":
            base_dir = self.features_dir
        elif bucket == "replay":
            base_dir = self.replay_dir
        else:
            raise ValueError(f"unsupported telemetry bucket: {bucket}")
        path = base_dir / "by_symbol" / symbol_storage_dirname(symbol) / relative_name
        self._append_jsonl_path(path, row)

    def read_csv_tail(self, path: Path, max_rows: int) -> pl.DataFrame | None:
        return self._read_csv_tail(path, max_rows)

    def _append_jsonl_path(self, path: Path, row: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        rotate_file_if_needed(path, self.rotation_max_mb)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=True, default=str) + "\n")

    def write_rejection_summary(self, cycle_id: str, rejections: dict[str, int]) -> None:
        self.append_jsonl(
            "rejections.jsonl",
            {
                "ts": datetime.now(UTC).isoformat(),
                "cycle_id": cycle_id,
                "rejections": dict(rejections),
            },
        )

    def append_calibration_snapshot(self, symbol: str, snapshot: dict[str, Any]) -> None:
        row = {
            "ts": datetime.now(UTC).isoformat(),
            "symbol": symbol,
            "funding_rate": snapshot.get("funding_rate"),
            "liquidation_notional_usd": snapshot.get(
                "liquidation_notional_usd", snapshot.get("liquidation_notional")
            ),
            "oi_growth_pct": snapshot.get("oi_growth_pct"),
            "volume_ratio_15m": snapshot.get("volume_ratio_15m"),
            "spread_bps": snapshot.get("spread_bps"),
            "asset_group": snapshot.get("asset_group"),
            "cycle_timestamp": snapshot.get("cycle_timestamp"),
        }
        if all(
            row[key] is None
            for key in (
                "funding_rate",
                "liquidation_notional_usd",
                "oi_growth_pct",
                "volume_ratio_15m",
                "spread_bps",
            )
        ):
            return
        self._append_jsonl_path(self.root_dir / "calibration_snapshots.jsonl", row)

    def persist_candles(self, symbol: str, timeframe: str, df: pl.DataFrame, max_rows: int) -> None:
        out_dir = self.market_dir / symbol_storage_dirname(symbol)
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"{timeframe}.csv"
        frame = df.clone()
        if frame.is_empty():
            return
        # Convert time columns to string for consistent CSV storage
        for column in ("time", "close_time"):
            if column in frame.columns:
                frame = frame.with_columns(pl.col(column).cast(pl.Utf8).alias(column))
        frame = frame.unique(subset=["time"], keep="last").sort("time")
        with self._csv_lock(path):
            if not path.exists() or path.stat().st_size == 0:
                initial = frame.tail(max_rows) if max_rows > 0 else frame
                initial.write_csv(path)
                self._remember_last_csv_time(path, initial)
                self._candle_last_time[str(path)] = str(initial.item(-1, "time"))
                return

            path_key = str(path)
            last_time = self._candle_last_time.get(path_key) or self._read_last_csv_time(path)
            first_new_time = str(frame.item(0, "time"))
            appended_avg_bytes = 0.0
            if last_time is None or first_new_time <= last_time:
                existing = self._read_csv_tail(path, max(max_rows * 3, 512))
                merged = frame if existing is None or existing.is_empty() else pl.concat(
                    [existing, frame],
                    how="diagonal_relaxed",
                )
                merged = merged.unique(subset=["time"], keep="last").sort("time")
                if max_rows > 0:
                    merged = merged.tail(max_rows)
                merged.write_csv(path)
                self._remember_last_csv_time(path, merged)
                if not merged.is_empty() and "time" in merged.columns:
                    self._candle_last_time[path_key] = str(merged.item(-1, "time"))
                self._candle_append_counts[path_key] = 0
                return

            # Fast path: the whole incoming frame is newer than the last stored
            # candle, so append without reading and rewriting the full CSV.
            if not frame.is_empty():
                csv_payload = frame.write_csv(include_header=False)
                appended_avg_bytes = len(csv_payload.encode("utf-8")) / max(frame.height, 1)
                with path.open("a", encoding="utf-8", newline="") as handle:
                    handle.write(csv_payload)
                self._remember_last_csv_time(path, frame)
                self._candle_last_time[path_key] = str(frame.item(-1, "time"))
                self._candle_append_counts[path_key] = (
                    self._candle_append_counts.get(path_key, 0) + 1
                )
            if max_rows > 0 and self._should_compact_csv(
                path,
                max_rows=max_rows,
                appended_avg_bytes=appended_avg_bytes,
            ):
                self._compact_csv(path, max_rows)
                tail = self._read_csv_tail(path, 1)
                self._remember_last_csv_time(path, tail)
                if tail is not None and not tail.is_empty() and "time" in tail.columns:
                    self._candle_last_time[path_key] = str(tail.item(-1, "time"))

    @staticmethod
    def _csv_lock(path: Path) -> threading.Lock:
        key = str(path)
        with _CSV_LOCKS_GUARD:
            lock = _CSV_LOCKS.get(key)
            if lock is None:
                lock = threading.Lock()
                _CSV_LOCKS[key] = lock
            return lock

    @staticmethod
    def _remember_last_csv_time(path: Path, frame: pl.DataFrame | None) -> None:
        if frame is None or frame.is_empty() or "time" not in frame.columns:
            return
        value = frame["time"].cast(pl.Utf8).tail(1).item()
        if value is not None:
            _CSV_LAST_TIME[str(path)] = str(value)

    @staticmethod
    def _read_last_csv_time(path: Path) -> str | None:
        if not path.exists() or path.stat().st_size == 0:
            return None
        try:
            with path.open("r", encoding="utf-8", newline="") as handle:
                reader = csv.DictReader(handle)
                last: str | None = None
                for row in reader:
                    raw = row.get("time")
                    if raw:
                        last = str(raw)
                if last:
                    _CSV_LAST_TIME[str(path)] = last
                return last
        except (OSError, csv.Error):
            return None

    @staticmethod
    def _should_compact_csv(
        path: Path,
        *,
        max_rows: int,
        appended_avg_bytes: float,
    ) -> bool:
        key = str(path)
        calls = _CSV_COMPACT_CALLS.get(key, 0) + 1
        _CSV_COMPACT_CALLS[key] = calls
        if calls % 10 == 0:
            return True
        if max_rows <= 0 or appended_avg_bytes <= 0.0:
            return False
        try:
            size = path.stat().st_size
        except OSError:
            return False
        expected_size = max_rows * max(appended_avg_bytes, 128.0)
        return size > expected_size * 2.0

    def _compact_csv(self, path: Path, max_rows: int) -> None:
        existing = self._read_csv_tail(path, max(max_rows * 3, 512))
        if existing is None or existing.is_empty():
            return
        existing = existing.unique(subset=["time"], keep="last").sort("time")
        if max_rows > 0:
            existing = existing.tail(max_rows)
        existing.write_csv(path)

    def _read_csv_tail(self, path: Path, max_rows: int) -> pl.DataFrame | None:
        if not path.exists():
            return None
        if max_rows <= 0:
            return pl.read_csv(path)
        # Polars doesn't have native tail reading - read all then tail
        try:
            df = pl.read_csv(path)
            if df.is_empty():
                return None
            if max_rows > 0:
                return df.tail(max_rows)
            return df
        except Exception as exc:
            LOG.debug("telemetry csv tail read failed | path=%s error=%s", path, exc)
            return None
