"""Ops webhook alert helper tests."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bot.domain.config import BotSettings, NotifierConfig, NotifierWebhookConfig
from bot.delivery.ops_webhook import ops_webhook_enabled, send_ops_webhook_alert


def _bot(*, ops_enabled: bool) -> SimpleNamespace:
    webhook = NotifierWebhookConfig(
        enabled=True,
        webhook_url="https://example.com/hook",
        ops_alerts_enabled=ops_enabled,
    )
    settings = BotSettings(
        tg_token="t",
        target_chat_id="1",
        notifiers=NotifierConfig(webhook=webhook),
    )
    return SimpleNamespace(settings=settings)


def test_ops_webhook_enabled_requires_flag() -> None:
    assert ops_webhook_enabled(_bot(ops_enabled=True)) is True  # type: ignore[arg-type]
    disabled = NotifierWebhookConfig(
        enabled=True,
        webhook_url="https://example.com/hook",
        ops_alerts_enabled=False,
    )
    # URL + ops_alerts auto-enables at load; test explicit disable without URL.
    disabled_no_url = NotifierWebhookConfig(
        enabled=True,
        webhook_url=None,
        ops_alerts_enabled=False,
    )
    settings = BotSettings(
        tg_token="t",
        target_chat_id="1",
        notifiers=NotifierConfig(webhook=disabled_no_url),
    )
    assert ops_webhook_enabled(SimpleNamespace(settings=settings)) is False  # type: ignore[arg-type]
    assert disabled.ops_alerts_enabled is True  # auto-enabled when URL set


@pytest.mark.asyncio
async def test_send_ops_webhook_posts_json() -> None:
    bot = _bot(ops_enabled=True)
    mock_resp = AsyncMock()
    mock_resp.status = 200
    mock_resp.text = AsyncMock(return_value="ok")
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)

    mock_session = MagicMock()
    mock_session.post = MagicMock(return_value=mock_resp)
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)

    with patch("bot.delivery.ops_webhook.aiohttp.ClientSession", return_value=mock_session):
        ok = await send_ops_webhook_alert(
            bot,  # type: ignore[arg-type]
            event="critical_error",
            text="<b>test</b> alert",
            extra={"k": 1},
        )
    assert ok is True
    mock_session.post.assert_called_once()
