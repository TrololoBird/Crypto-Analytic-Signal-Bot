from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import shutil
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import polars as pl

from bot.runtime.errors import DEFENSIVE_EXC

if TYPE_CHECKING:
    from pathlib import Path

LOG = logging.getLogger("bot.telemetry")


def _parse_iso_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def run_dir_started_at(run_dir: Path) -> datetime:
    """Return run start time from ``run_metadata.json``, else directory mtime."""
    metadata_path = run_dir / "run_metadata.json"
    if metadata_path.exists():
        try:
            payload = json.loads(metadata_path.read_text(encoding="utf-8"))
            started = _parse_iso_datetime(payload.get("started_at"))
            if started is not None:
                return started
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
            LOG.debug("run_metadata started_at read failed | path=%s error=%s", metadata_path, exc)
    try:
        return datetime.fromtimestamp(run_dir.stat().st_mtime, tz=UTC)
    except OSError:
        return datetime.now(UTC)


def slim_message_buffer_fields(snapshot: Mapping[str, Any] | None) -> dict[str, int]:
    """Extract compact WS buffer counters for cycles.jsonl and live runtime."""
    if not isinstance(snapshot, Mapping):
        return {}
    buf = snapshot.get("message_buffer")
    if isinstance(buf, Mapping):
        return {
            "message_buffer_size": int(buf.get("size") or 0),
            "message_buffer_dropped": int(buf.get("dropped") or 0),
        }
    size = snapshot.get("message_buffer_size")
    dropped = snapshot.get("message_buffer_dropped")
    if size is None and dropped is None:
        buffer_count = snapshot.get("buffer_message_count")
        if buffer_count is not None:
            size = buffer_count
    if size is None and dropped is None:
        return {}
    return {
        "message_buffer_size": int(size or 0),
        "message_buffer_dropped": int(dropped or 0),
    }


def apply_slim_message_buffer(row: dict[str, Any]) -> None:
    """Promote buffer counters to top-level keys and drop nested ``message_buffer``."""
    slim = slim_message_buffer_fields(row)
    if slim:
        row.update(slim)
    row.pop("message_buffer", None)


def _iter_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            rows.append(payload)
    return rows


def _cycle_delivery_success_count(row: Mapping[str, Any]) -> int:
    success = row.get("delivery_success_count")
    if success is not None:
        try:
            return int(success)
        except (TypeError, ValueError):
            pass
    for key in ("delivered_count", "delivered_signals"):
        value = row.get(key)
        if value is not None:
            try:
                return int(value)
            except (TypeError, ValueError):
                continue
    return 0


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


def _jsonl_append_line(path: Path, line: str, *, max_size_mb: int) -> None:
    """Sync JSONL append (safe to run via ``asyncio.to_thread``)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    rotate_file_if_needed(path, max_size_mb)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line)


def rotate_file_if_needed(path: Path, max_size_mb: int) -> None:
    if max_size_mb <= 0 or not path.exists():
        return
    max_bytes = max_size_mb * 1024 * 1024
    if path.stat().st_size <= max_bytes:
        return
    stamp = datetime.now(UTC).date().isoformat()
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
        self.analysis_dir.mkdir(parents=True, exist_ok=True)
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        self.features_dir.mkdir(parents=True, exist_ok=True)
        self.replay_dir.mkdir(parents=True, exist_ok=True)
        self._async_tasks: set[asyncio.Task[Any]] = set()
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
            msg = f"unsupported telemetry bucket: {bucket}"
            raise ValueError(msg)
        path = base_dir / "by_symbol" / symbol_storage_dirname(symbol) / relative_name
        self._append_jsonl_path(path, row)

    def read_csv_tail(self, path: Path, max_rows: int) -> pl.DataFrame | None:
        return self._read_csv_tail(path, max_rows)

    def _append_jsonl_path(self, path: Path, row: dict[str, Any]) -> None:
        if self.run_id and "run_id" not in row:
            row = {**row, "run_id": self.run_id}
        line = json.dumps(row, ensure_ascii=True, default=str) + "\n"
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            _jsonl_append_line(path, line, max_size_mb=self.rotation_max_mb)
        else:
            task = loop.create_task(
                asyncio.to_thread(
                    _jsonl_append_line,
                    path,
                    line,
                    max_size_mb=self.rotation_max_mb,
                )
            )
            self._async_tasks.add(task)
            task.add_done_callback(self._async_tasks.discard)

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
        calibration_path = (
            self.base_dir / "calibration_snapshots.jsonl"
            if self.run_id
            else self.root_dir / "calibration_snapshots.jsonl"
        )
        self._append_jsonl_path(calibration_path, row)

    def collect_session_totals(self, *, extras: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """Aggregate session counters from analysis JSONL for run metadata."""
        cycles = _iter_jsonl(self.analysis_dir / "cycles.jsonl")
        delivery_rows = _iter_jsonl(self.analysis_dir / "delivery.jsonl")
        delivery_success_statuses = frozenset({"sent", "logged"})
        delivery_success = sum(
            1
            for row in delivery_rows
            if str(row.get("delivery_status") or row.get("status") or "").lower()
            in delivery_success_statuses
        )
        totals: dict[str, Any] = {
            "cycles": len(cycles),
            "candidates": sum(int(row.get("candidate_count") or 0) for row in cycles),
            "selected": sum(
                int(row.get("selected_count") or row.get("selected_signals") or 0) for row in cycles
            ),
            "delivered_cycles": sum(_cycle_delivery_success_count(row) for row in cycles),
            "delivery_success": delivery_success,
            "delivery_attempts": len(delivery_rows),
            "rejected": sum(int(row.get("rejected_count") or 0) for row in cycles),
        }
        if extras:
            totals.update(dict(extras))
        return totals

    def finalize_run_metadata(self, *, session_totals: Mapping[str, Any]) -> None:
        """Persist ``ended_at`` and session totals when a run-scoped store shuts down."""
        if not self.run_id:
            return
        metadata_path = self.base_dir / "run_metadata.json"
        payload: dict[str, Any] = {
            "run_id": self.run_id,
            "started_at": self.started_at.isoformat(),
            "schema_version": 2,
        }
        if metadata_path.exists():
            try:
                existing = json.loads(metadata_path.read_text(encoding="utf-8"))
                if isinstance(existing, dict):
                    payload.update(existing)
            except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
                LOG.debug("run_metadata merge read failed | path=%s error=%s", metadata_path, exc)
        payload["ended_at"] = datetime.now(UTC).isoformat()
        payload["session_totals"] = dict(session_totals)
        metadata_path.parent.mkdir(parents=True, exist_ok=True)
        metadata_path.write_text(
            json.dumps(payload, indent=2, ensure_ascii=True),
            encoding="utf-8",
        )

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
        except DEFENSIVE_EXC as exc:
            LOG.debug("telemetry csv tail read failed | path=%s error=%s", path, exc)
            return None
        else:
            return df
