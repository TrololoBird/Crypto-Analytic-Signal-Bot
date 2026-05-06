from __future__ import annotations

import pytest

from bot.config import BotSettings
from bot.messaging import (
    DeliveryResult,
    DisabledBroadcaster,
    WebhookBroadcaster,
    build_message_broadcaster,
)


def test_bot_settings_accepts_fractional_sl_buffer_atr() -> None:
    settings = BotSettings(
        tg_token="1" * 30,
        target_chat_id="123",
        filters={"setups": {"ema_bounce": {"sl_buffer_atr": 0.5}}},
    )
    assert settings.filters.setups["ema_bounce"]["sl_buffer_atr"] == pytest.approx(0.5)


def test_bot_settings_rejects_too_small_sl_buffer_atr() -> None:
    with pytest.raises(ValueError):
        BotSettings(
            tg_token="1" * 30,
            target_chat_id="123",
            filters={"setups": {"ema_bounce": {"sl_buffer_atr": 0.01}}},
        )


@pytest.mark.asyncio
async def test_build_message_broadcaster_supports_disabled_provider() -> None:
    settings = BotSettings(tg_token="", target_chat_id="", notifiers={"provider": "none"})
    broadcaster = build_message_broadcaster(settings)

    assert isinstance(broadcaster, DisabledBroadcaster)
    assert await broadcaster.send_html("<b>Hello</b>") == DeliveryResult(
        status="logged", message_id=None, reason="notifier_disabled"
    )


@pytest.mark.asyncio
async def test_build_message_broadcaster_supports_webhook_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent: list[tuple[str, dict, dict]] = []

    class _Response:
        status = 200

        async def text(self) -> str:
            return "ok"

    class _Context:
        async def __aenter__(self):
            return _Response()

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

    class _Session:
        def post(self, url, *, json, headers, timeout):
            sent.append((url, json, headers))
            return _Context()

        async def close(self) -> None:
            return None

    monkeypatch.setattr("bot.messaging.aiohttp.ClientSession", lambda: _Session())

    settings = BotSettings(
        tg_token="",
        target_chat_id="",
        notifiers={
            "provider": "slack",
            "slack": {"enabled": True, "webhook_url": "https://example.test/webhook"},
        },
    )
    broadcaster = build_message_broadcaster(settings)
    assert isinstance(broadcaster, WebhookBroadcaster)

    result = await broadcaster.send_html("<b>Hello</b>")

    assert result == DeliveryResult(status="sent", message_id=None, reason=None)
    assert sent[0][0] == "https://example.test/webhook"
    assert sent[0][1]["text"] == "Hello"
    await broadcaster.close()
