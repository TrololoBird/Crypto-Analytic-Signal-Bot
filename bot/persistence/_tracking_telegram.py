"""Telegram message-id linkage for SignalTracker (Phase G)."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from bot.runtime.errors import DEFENSIVE_EXC

if TYPE_CHECKING:
    from ..domain.schemas import Signal
    from ..persistence.tracked import TrackedSignalState


LOG = logging.getLogger("bot.tracking")


class TelegramTrackingMixin:
    """Arms signals with Telegram ids and patches message_id after delivery."""

    async def arm_signals_with_messages(
        self,
        signals: list[Signal],
        *,
        dry_run: bool,
        message_ids: dict[str, int | None],
    ) -> None:
        if dry_run or not self.settings.tracking.enabled or not signals:
            return
        now = datetime.now(UTC)
        for signal in signals:
            tracked = await self._arm_signal(
                signal,
                signal_message_id=message_ids.get(signal.tracking_id),
            )
            self.telemetry.append_jsonl(
                "tracking_events.jsonl",
                {
                    "ts": now.isoformat(),
                    "event_type": "armed",
                    "tracking_id": tracked.tracking_id,
                    "tracking_ref": tracked.tracking_ref,
                    "signal_key": tracked.signal_key,
                    "symbol": tracked.symbol,
                    "setup_id": tracked.setup_id,
                    "direction": tracked.direction,
                    "entry_low": tracked.entry_low,
                    "entry_high": tracked.entry_high,
                    "stop": tracked.stop,
                    "take_profit_1": tracked.take_profit_1,
                    "take_profit_2": tracked.take_profit_2,
                    "take_profit_3": tracked.take_profit_3,
                    "valid_until": tracked.valid_until,
                    "scale_weights": tracked.scale_weights,
                    "single_target_mode": tracked.single_target_mode,
                    "target_integrity_status": tracked.target_integrity_status,
                    "signal_message_id": tracked.signal_message_id,
                    "stats": await self._stats_snapshot(),
                },
            )
        await self._persist_tracking_state()
    async def update_signal_message_ids(
        self,
        message_ids: dict[str, int | None],
        *,
        dry_run: bool,
    ) -> None:
        """Attach Telegram message ids after delivery (journal row already armed)."""
        if dry_run or not self.settings.tracking.enabled or not message_ids:
            return
        now = datetime.now(UTC)
        active_rows = await self.memory_repo.get_active_signals()
        by_tracking_id = {
            str(row.get("tracking_id") or ""): row for row in active_rows if row.get("tracking_id")
        }
        for tracking_id, message_id in message_ids.items():
            if message_id is None:
                continue
            row = by_tracking_id.get(str(tracking_id))
            if row is None:
                continue
            try:
                tracked = self._tracked_from_payload(row)
            except (TypeError, ValueError) as exc:
                LOG.debug("message_id patch skipped %s: %s", tracking_id, exc)
                continue
            if tracked.signal_message_id == message_id:
                continue
            tracked.signal_message_id = message_id
            await self.memory_repo.save_active_signal(self._tracked_to_payload(tracked))
            self.telemetry.append_jsonl(
                "tracking_events.jsonl",
                {
                    "ts": now.isoformat(),
                    "event_type": "message_linked",
                    "tracking_id": tracked.tracking_id,
                    "signal_message_id": message_id,
                    "symbol": tracked.symbol,
                    "setup_id": tracked.setup_id,
                },
            )
        await self._persist_tracking_state()
