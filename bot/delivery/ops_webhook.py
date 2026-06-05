"""Ops webhook alerts - parallel to Telegram operator DMs (PagerDuty/Slack generic hook)."""

from __future__ import annotations

import html
import logging
import re
from typing import TYPE_CHECKING, Any

import aiohttp

from bot.runtime.errors import DEFENSIVE_EXC

if TYPE_CHECKING:
    from bot.runtime.bot import SignalBot

LOG = logging.getLogger("bot.delivery.ops_webhook")

_TAG_RE = re.compile(r"<[^>]+>")


def _plain_text(html_text: str) -> str:
    stripped = _TAG_RE.sub("", html_text or "").replace("&nbsp;", " ")
    return html.unescape(stripped).strip()


def ops_webhook_enabled(bot: SignalBot) -> bool:
    cfg = getattr(getattr(bot.settings, "notifiers", None), "webhook", None)
    return bool(
        cfg
        and getattr(cfg, "enabled", False)
        and getattr(cfg, "ops_alerts_enabled", False)
        and getattr(cfg, "webhook_url", None)
    )


def _ops_webhook_session(bot: SignalBot) -> aiohttp.ClientSession:
    session = getattr(bot, "_ops_webhook_session", None)
    if session is None or session.closed:
        session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=12))
        bot._ops_webhook_session = session
    return session


async def close_ops_webhook_session(bot: SignalBot) -> None:
    session = getattr(bot, "_ops_webhook_session", None)
    if session is not None and not session.closed:
        await session.close()
    bot._ops_webhook_session = None


async def send_ops_webhook_alert(
    bot: SignalBot,
    *,
    event: str,
    text: str,
    extra: dict[str, Any] | None = None,
) -> bool:
    """POST JSON alert to ``[bot.notifiers.webhook]`` when ops_alerts_enabled."""
    if not ops_webhook_enabled(bot):
        return False
    cfg = bot.settings.notifiers.webhook
    url = str(cfg.webhook_url or "").strip()
    if not url:
        return False

    payload: dict[str, Any] = {
        "event": str(event),
        "text": _plain_text(text),
        "html": text,
        "source": "crypto-signal-bot",
        "extra": dict(extra or {}),
    }
    if cfg.username:
        payload["username"] = cfg.username

    headers = {"Content-Type": "application/json"}
    token = getattr(cfg, "bearer_token", None)
    if token:
        headers["Authorization"] = f"Bearer {token}"

    try:
        session = _ops_webhook_session(bot)
        async with session.post(url, json=payload, headers=headers) as response:
            if response.status >= 400:
                body = await response.text()
                LOG.warning(
                    "ops webhook alert failed | event=%s status=%s body=%s",
                    event,
                    response.status,
                    body[:200],
                )
                return False
    except DEFENSIVE_EXC:
        LOG.debug("ops webhook alert error | event=%s", event, exc_info=True)
        return False

    LOG.info("ops webhook alert sent | event=%s", event)
    return True


async def notify_ops_delivery_failed(
    bot: SignalBot,
    *,
    symbol: str,
    setup_id: str,
    direction: str,
    reason: str,
    delivery_reason: str | None = None,
) -> bool:
    text = (
        "<b>⚠️ Delivery failed</b>\n"
        f"{html.escape(symbol)} {html.escape(direction)} · {html.escape(setup_id)}\n"
        f"Reason <code>{html.escape(reason)}</code>"
    )
    if delivery_reason:
        text += f"\nDetail <code>{html.escape(delivery_reason)}</code>"
    return await send_ops_webhook_alert(
        bot,
        event="delivery_failed",
        text=text,
        extra={
            "symbol": symbol,
            "setup_id": setup_id,
            "direction": direction,
            "reason": reason,
            "delivery_reason": delivery_reason,
        },
    )


async def notify_ops_tier_cap_starvation(
    bot: SignalBot,
    *,
    symbol: str,
    setup_id: str,
    direction: str,
    tier: str,
    drop_reason: str,
) -> bool:
    text = (
        "<b>⚠️ Tier cap starvation</b>\n"
        f"{html.escape(symbol)} {html.escape(direction)} · {html.escape(setup_id)}\n"
        f"Tier <code>{html.escape(tier)}</code> · drop <code>{html.escape(drop_reason)}</code>"
    )
    return await send_ops_webhook_alert(
        bot,
        event="tier_cap_starvation",
        text=text,
        extra={
            "symbol": symbol,
            "setup_id": setup_id,
            "direction": direction,
            "tier": tier,
            "drop_reason": drop_reason,
        },
    )
