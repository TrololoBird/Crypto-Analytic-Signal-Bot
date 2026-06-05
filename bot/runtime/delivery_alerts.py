"""Operator alerts for delivery health (zero-delivery streaks)."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any

from bot.delivery.ops_webhook import send_ops_webhook_alert
from bot.delivery.telegram_routing import operator_dm_enabled, send_operator_html
from bot.runtime.errors import DEFENSIVE_EXC

if TYPE_CHECKING:
    from bot.runtime.bot import SignalBot

LOG = logging.getLogger("bot.runtime.delivery_alerts")


def _update_zero_delivery_streak(bot: SignalBot, *, delivered_count: int) -> int:
    streak = int(getattr(bot, "_zero_delivery_streak", 0) or 0)
    if delivered_count > 0:
        bot._zero_delivery_streak = 0
        return 0
    streak += 1
    bot._zero_delivery_streak = streak
    return streak


async def _send_zero_delivery_alert(bot: SignalBot, *, streak: int) -> None:
    threshold = int(getattr(bot.settings.delivery, "zero_delivery_alert_cycles", 0) or 0)
    if threshold <= 0 or streak < threshold:
        return

    last_alert = float(getattr(bot, "_last_zero_delivery_alert_mono", 0.0) or 0.0)
    now = time.monotonic()
    if last_alert and (now - last_alert) < 3600.0:
        return

    summary = getattr(bot, "last_cycle_summary", {}) or {}
    text = (
        "<b>⚠️ Zero delivery streak</b>\n"
        f"Циклов подряд без отправки: <code>{streak}</code>\n"
        f"Кандидатов (посл. цикл): "
        f"<code>{summary.get('candidates') or summary.get('post_filter_candidates') or 0}</code>\n"
        f"Shortlist: <code>{summary.get('shortlist_size') or len(bot._shortlist)}</code>\n"
        "<i>Operator alert · проверьте /funnel и /health</i>"
    )

    notified = False
    if operator_dm_enabled(bot, "send_critical_alerts"):
        try:
            notified = bool(await send_operator_html(bot, text))
        except DEFENSIVE_EXC:
            LOG.debug("zero delivery telegram alert failed", exc_info=True)

    if await send_ops_webhook_alert(
        bot,
        event="zero_delivery_streak",
        text=text,
        extra={"streak": streak, "threshold": threshold},
    ):
        notified = True

    if notified:
        bot._last_zero_delivery_alert_mono = now
        LOG.warning(
            "zero delivery streak alert sent | streak=%d threshold=%d",
            streak,
            threshold,
        )


def record_cycle_delivery_outcome(bot: SignalBot, *, delivered_count: int) -> None:
    """Sync hook from cycle telemetry — schedules operator alert when streak threshold hit."""
    streak = _update_zero_delivery_streak(bot, delivered_count=delivered_count)
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    bg_task = loop.create_task(_send_zero_delivery_alert(bot, streak=streak))
    bot._background_tasks.add(bg_task)
    bg_task.add_done_callback(bot._background_tasks.discard)


def _message_buffer_dropped_total(ws_snapshot: dict[str, Any]) -> int:
    buf = ws_snapshot.get("message_buffer")
    if isinstance(buf, dict):
        return int(buf.get("dropped") or 0)
    return 0


async def check_message_buffer_drop_alert(
    bot: SignalBot,
    *,
    ws_snapshot: dict[str, Any],
) -> None:
    """Alert operators when WS message_buffer.dropped jumps beyond threshold per interval."""
    threshold = int(getattr(bot.settings.ws, "message_buffer_drop_alert_threshold", 0) or 0)
    if threshold <= 0:
        return

    dropped = _message_buffer_dropped_total(ws_snapshot)
    if not getattr(bot, "_message_buffer_drop_baseline_set", False):
        bot._message_buffer_drop_baseline_set = True
        bot._last_message_buffer_dropped = dropped
        return

    prev = int(getattr(bot, "_last_message_buffer_dropped", 0) or 0)
    bot._last_message_buffer_dropped = dropped
    delta = dropped - prev
    if delta <= threshold:
        return

    last_alert = float(getattr(bot, "_last_message_buffer_drop_alert_mono", 0.0) or 0.0)
    now = time.monotonic()
    if last_alert and (now - last_alert) < 3600.0:
        return

    buf = ws_snapshot.get("message_buffer")
    buf_size = int(buf.get("size") or 0) if isinstance(buf, dict) else 0
    text = (
        "<b>⚠️ WS message buffer drops</b>\n"
        f"Прирост dropped за интервал: <code>{delta}</code> (порог <code>{threshold}</code>)\n"
        f"Всего dropped: <code>{dropped}</code> · buffer size: <code>{buf_size}</code>\n"
        "<i>Operator alert · проверьте /health и WS lag</i>"
    )

    notified = False
    if operator_dm_enabled(bot, "send_critical_alerts"):
        try:
            notified = bool(await send_operator_html(bot, text))
        except DEFENSIVE_EXC:
            LOG.debug("message buffer drop telegram alert failed", exc_info=True)

    if await send_ops_webhook_alert(
        bot,
        event="message_buffer_drops",
        text=text,
        extra={"delta": delta, "dropped_total": dropped, "threshold": threshold},
    ):
        notified = True

    if notified:
        bot._last_message_buffer_drop_alert_mono = now
        LOG.warning(
            "message buffer drop alert sent | delta=%d dropped_total=%d threshold=%d",
            delta,
            dropped,
            threshold,
        )
