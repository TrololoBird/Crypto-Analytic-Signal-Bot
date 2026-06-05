"""Tracking lifecycle events (shared by tracker and TP/SL review mixin)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Mapping

    from bot.persistence.tracked import TrackedSignalState


@dataclass(frozen=True, slots=True)
class SignalTrackingEvent:
    """Event representing a state change in signal tracking."""

    event_type: str
    tracked: TrackedSignalState
    occurred_at: datetime
    event_price: float | None
    precision_mode: str
    note: str | None = None

    def to_log_row(self, *, stats: Mapping[str, float | int]) -> dict[str, Any]:
        """Convert event to loggable dictionary format."""
        return {
            "ts": self.occurred_at.astimezone(UTC).isoformat(),
            "event_type": self.event_type,
            "lifecycle_event": self.event_type,
            "tracking_semantics": "tracked_signal_lifecycle",
            "runtime_mode": "signal_only",
            "exchange_execution": False,
            "tracking_id": self.tracked.tracking_id,
            "tracking_ref": self.tracked.tracking_ref,
            "signal_key": self.tracked.signal_key,
            "symbol": self.tracked.symbol,
            "setup_id": self.tracked.setup_id,
            "direction": self.tracked.direction,
            "status": self.tracked.status,
            "close_reason": self.tracked.close_reason,
            "event_price": self.event_price,
            "precision_mode": self.precision_mode,
            "note": self.note,
            "entry_low": self.tracked.entry_low,
            "entry_high": self.tracked.entry_high,
            "stop": self.tracked.stop,
            "take_profit_1": self.tracked.take_profit_1,
            "take_profit_2": self.tracked.take_profit_2,
            "take_profit_3": self.tracked.take_profit_3,
            "valid_until": self.tracked.valid_until,
            "scale_weights": self.tracked.scale_weights,
            "single_target_mode": self.tracked.single_target_mode,
            "target_integrity_status": self.tracked.target_integrity_status,
            "created_at": self.tracked.created_at,
            "activated_at": self.tracked.activated_at,
            "tp1_hit_at": self.tracked.tp1_hit_at,
            "closed_at": self.tracked.closed_at,
            "stats": stats,
        }
