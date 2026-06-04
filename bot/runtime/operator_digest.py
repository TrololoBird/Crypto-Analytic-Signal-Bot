"""Periodic operator digest to Telegram (DM to authorized operators)."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from bot.runtime.errors import DEFENSIVE_EXC

from ..dashboard.mobile_summary import build_mobile_summary, format_mobile_digest_text
from .telegram_operator import TelegramOperatorConsole, operator_console_enabled

if TYPE_CHECKING:
    from ..runtime.bot import SignalBot

LOG = logging.getLogger("bot.operator_digest")


class OperatorDigestRunner:
    def __init__(self, bot: SignalBot) -> None:
        self._bot = bot
        self._last_sent_ts: float = 0.0
        self._console: TelegramOperatorConsole | None = None

    def _get_console(self) -> TelegramOperatorConsole | None:
        if not operator_console_enabled(self._bot):
            return None
        if self._console is None:
            self._console = TelegramOperatorConsole(self._bot)
        return self._console

    async def maybe_send_digest(self, *, interval_seconds: float = 1800.0) -> None:
        import time

        from bot.delivery.telegram_routing import operator_dm_enabled

        if not operator_dm_enabled(self._bot, "send_digest"):
            return
        op_cfg = getattr(self._bot.settings.notifiers, "telegram_operator", None)
        interval = float(getattr(op_cfg, "digest_interval_seconds", interval_seconds) or interval_seconds)

        now = time.monotonic()
        if self._last_sent_ts and (now - self._last_sent_ts) < max(300.0, interval):
            return

        dashboard = getattr(self._bot, "dashboard", None)
        live_data = getattr(dashboard, "_live_data", None) if dashboard is not None else None
        if live_data is None:
            return

        try:
            payload = await build_mobile_summary(self._bot, live_data)
            text = format_mobile_digest_text(payload)
        except DEFENSIVE_EXC:
            LOG.debug("operator digest build skipped", exc_info=True)
            return

        sent_count = 0
        console = self._get_console()
        if console is not None:
            sent_count = await console.send_html_to_operators(text)

        if sent_count > 0:
            self._last_sent_ts = now
            LOG.info(
                "operator digest sent | targets=%s wins=%s sl=%s",
                sent_count,
                (payload.get("outcomes_7d") or {}).get("wins"),
                (payload.get("outcomes_7d") or {}).get("stop_losses"),
            )
