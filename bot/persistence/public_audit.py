"""Public audit ledger — daily CSV + SHA256 (target spec P3)."""

from __future__ import annotations

import asyncio
import csv
import hashlib
import logging
from collections import deque
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from threading import Lock
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

    from bot.domain.schemas import Signal

LOG = logging.getLogger("bot.persistence.public_audit")

_CSV_FIELDS = (
    "ts_utc",
    "tracking_id",
    "symbol",
    "setup_id",
    "direction",
    "score",
    "tier",
    "entry_mid",
    "stop_loss",
    "tp1",
    "risk_reward",
    "message_id",
)


@dataclass(frozen=True, slots=True)
class AuditRow:
    ts_utc: str
    tracking_id: str
    symbol: str
    setup_id: str
    direction: str
    score: float
    tier: str
    entry_mid: float | None
    stop_loss: float | None
    tp1: float | None
    risk_reward: float | None
    message_id: int | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "ts_utc": self.ts_utc,
            "tracking_id": self.tracking_id,
            "symbol": self.symbol,
            "setup_id": self.setup_id,
            "direction": self.direction,
            "score": self.score,
            "tier": self.tier,
            "entry_mid": self.entry_mid,
            "stop_loss": self.stop_loss,
            "tp1": self.tp1,
            "risk_reward": self.risk_reward,
            "message_id": self.message_id,
        }


class PublicAuditLedger:
    """Append-only daily CSV with SHA256 sidecar for subscriber verification."""

    def __init__(self, root_dir: Path, *, enabled: bool = True) -> None:
        self._root = root_dir
        self._enabled = enabled
        self._lock = Lock()
        self._action_history: deque[tuple[Signal, datetime]] = deque(maxlen=256)
        if enabled:
            self._root.mkdir(parents=True, exist_ok=True)

    def recent_action_signals(
        self,
        *,
        within_hours: float = 4.0,
        now: datetime | None = None,
    ) -> list[Signal]:
        """Delivered ACTION signals within the merge conflict window."""
        resolved = (now or datetime.now(UTC)).astimezone(UTC)
        cutoff = resolved - timedelta(hours=max(0.0, within_hours))
        return [signal for signal, delivered_at in self._action_history if delivered_at >= cutoff]

    def _paths_for_day(self, day: datetime) -> tuple[Path, Path]:
        stamp = day.strftime("%Y%m%d")
        csv_path = self._root / f"signals_{stamp}.csv"
        sha_path = self._root / f"signals_{stamp}.sha256"
        return csv_path, sha_path

    def append_delivered(
        self,
        signal: Signal,
        *,
        tier: str,
        message_id: int | None,
    ) -> None:
        if not self._enabled:
            return
        row = AuditRow(
            ts_utc=datetime.now(UTC).isoformat(),
            tracking_id=str(signal.tracking_id),
            symbol=str(signal.symbol),
            setup_id=str(signal.setup_id),
            direction=str(signal.direction),
            score=float(signal.score or 0.0),
            tier=str(tier),
            entry_mid=_optional_float(signal.entry_mid),
            stop_loss=_optional_float(signal.stop_loss),
            tp1=_optional_float(signal.take_profit_1),
            risk_reward=_optional_float(signal.risk_reward),
            message_id=message_id,
        )
        self._append_row(row)
        if str(tier).lower() == "action":
            delivered_at = datetime.now(UTC)
            self._action_history.append((replace(signal, created_at=delivered_at), delivered_at))

    def _append_row(self, row: AuditRow) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self._append_row_sync(row)
        else:
            loop.create_task(asyncio.to_thread(self._append_row_sync, row))

    def _append_row_sync(self, row: AuditRow) -> None:
        with self._lock:
            try:
                csv_path, sha_path = self._paths_for_day(datetime.now(UTC))
                write_header = not csv_path.exists()
                with csv_path.open("a", encoding="utf-8", newline="") as handle:
                    writer = csv.DictWriter(handle, fieldnames=_CSV_FIELDS)
                    if write_header:
                        writer.writeheader()
                    writer.writerow(row.as_dict())
                digest = hashlib.sha256(csv_path.read_bytes()).hexdigest()
                sha_path.write_text(f"{digest}  {csv_path.name}\n", encoding="utf-8")
            except OSError as exc:
                LOG.warning("public_audit_append_failed: %s", exc)

    def latest_manifest(self) -> dict[str, Any]:
        """Return newest daily file paths and digests for dashboard/API."""
        if not self._root.exists():
            return {"enabled": self._enabled, "files": []}
        files: list[dict[str, Any]] = []
        for csv_path in sorted(self._root.glob("signals_*.csv"), reverse=True)[:7]:
            sha_path = self._root / f"{csv_path.stem}.sha256"
            digest = None
            if sha_path.exists():
                digest = sha_path.read_text(encoding="utf-8").split(maxsplit=1)[0]
            files.append(
                {
                    "csv": str(csv_path),
                    "sha256": digest,
                    "rows": max(0, sum(1 for _ in csv_path.open(encoding="utf-8")) - 1),
                }
            )
        return {"enabled": self._enabled, "root": str(self._root), "files": files}


def _optional_float(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None
