"""Telegram routing: channel (subscribers) vs operator private DM (ops).

Channel ``TELEGRAM_CHAT_ID`` - trading signals only:
  - ACTION/WATCH signal cards
  - In-place signal card status edits
  - Subscriber tracking updates (TP/SL/activation - concise)

Operator DM ``TELEGRAM_OPERATOR_USER_IDS`` - monitoring & control:
  - /command replies, digests, market context, startup reports
  - Detailed SL post-mortem analytics
  - Critical runtime alerts
  - WATCH analytics companion (when enabled)
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from bot.delivery.deliver import format_analytics_companion
from bot.runtime.errors import DEFENSIVE_EXC
from bot.runtime.telegram_operator import TelegramOperatorConsole, operator_console_enabled

if TYPE_CHECKING:
    from bot.domain.schemas import Signal
    from bot.runtime.bot import SignalBot

LOG = logging.getLogger("bot.delivery.telegram_routing")

CHANNEL_PURPOSE = (
    "Канал: только сигналы и статусы сделок для подписчиков. Оператор: личка с ботом - /help"
)
OPERATOR_PURPOSE = (
    "Личка оператора: мониторинг и команды (/market /status /audit …). "
    "В канал ops-сообщения не отправляются."
)


async def send_operator_html(bot: SignalBot, text: str) -> int:
    """Send HTML to all authorized operator user ids (private DM)."""
    if not operator_console_enabled(bot):
        LOG.debug("operator send skipped | no token or TELEGRAM_OPERATOR_USER_IDS")
        return 0
    console = getattr(bot, "_operator_console", None)
    if console is None:
        console = TelegramOperatorConsole(bot)
    try:
        sent = await console.send_html_to_operators(text)
    except DEFENSIVE_EXC:
        LOG.debug("operator DM send failed", exc_info=True)
        return 0
    else:
        if sent:
            LOG.info("operator DM sent | recipients=%s chars=%s", sent, len(text))
        return sent


def operator_dm_enabled(bot: SignalBot, flag_name: str, *, default: bool = True) -> bool:
    op_cfg = getattr(getattr(bot.settings, "notifiers", None), "telegram_operator", None)
    if op_cfg is not None and not bool(getattr(op_cfg, "enabled", True)):
        return False
    return bool(getattr(op_cfg, flag_name, default))


def should_send_channel_analytics_companion(
    notifier_settings: Any,
    *,
    tier: str,
) -> bool:
    """Return True when analytics companion should go to the signal channel."""
    if not bool(getattr(notifier_settings, "send_analytics_companion", False)):
        return False
    if bool(getattr(notifier_settings, "analytics_companion_action_only", False)):
        return str(tier or "").lower() == "action"
    return True


async def send_operator_analytics_companion(
    bot: SignalBot,
    signal: Signal,
    *,
    btc_bias: str | None = None,
    eth_bias: str | None = None,
) -> int:
    """Send analytics companion narrative to operator DMs (WATCH tier companion)."""
    if not operator_dm_enabled(bot, "send_watch_companion"):
        return 0
    text = format_analytics_companion(signal, btc_bias=btc_bias, eth_bias=eth_bias)
    return await send_operator_html(bot, text)
