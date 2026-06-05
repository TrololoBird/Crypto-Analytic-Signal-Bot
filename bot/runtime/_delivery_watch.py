"""Watch-tier and delivery telemetry recording (extracted from delivery_orchestrator)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bot.domain.schemas import Signal
    from bot.runtime.bot import SignalBot


class DeliveryWatchMixin:
    """Watch screener JSONL and delivery attempt journaling."""

    _bot: SignalBot

    def _record_watch_screener(
        self,
        signal: Signal,
        *,
        tier: str,
        tier_reason: str,
    ) -> None:
        if tier != "watch":
            return
        if not bool(getattr(self._bot.settings.delivery, "watch_screener_enabled", True)):
            return
        telemetry = getattr(self._bot, "telemetry", None)
        append_jsonl = getattr(telemetry, "append_jsonl", None)
        if not callable(append_jsonl):
            return
        append_jsonl(
            "watch_screener.jsonl",
            {
                "ts": datetime.now(UTC).isoformat(),
                **signal.to_log_row(),
                "tier": tier,
                "tier_reason": tier_reason,
            },
        )
    def _record_delivery_attempt(
        self,
        signal: Signal,
        *,
        status: str,
        reason: str | None,
        message_id: int | None,
    ) -> None:
        telemetry = getattr(self._bot, "telemetry", None)
        append_jsonl = getattr(telemetry, "append_jsonl", None)
        if not callable(append_jsonl):
            return
        append_jsonl(
            "delivery.jsonl",
            {
                "ts": datetime.now(UTC).isoformat(),
                **signal.to_log_row(),
                "delivery_status": status,
                "delivery_reason": reason,
                "message_id": message_id,
            },
        )
