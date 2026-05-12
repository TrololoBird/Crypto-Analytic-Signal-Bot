from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from bot.application.bot import SignalBot
from bot.delivery import SignalDelivery
from bot.domain.config import BotSettings
from bot.domain.schemas import Signal
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
    settings = BotSettings(
        tg_token="", target_chat_id="", notifiers={"provider": "none"}
    )
    broadcaster = build_message_broadcaster(settings)

    assert isinstance(broadcaster, DisabledBroadcaster)
    assert await broadcaster.send_html("<b>Hello</b>") == DeliveryResult(
        status="logged", message_id=None, reason="notifier_disabled"
    )


@pytest.mark.asyncio
async def test_disabled_broadcaster_preflight_fails_loudly() -> None:
    broadcaster = DisabledBroadcaster()

    with pytest.raises(RuntimeError, match="notifier provider is disabled"):
        await broadcaster.preflight_check()


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


@pytest.mark.asyncio
async def test_signal_delivery_warns_when_message_status_is_not_sent(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class LoggedBroadcaster:
        async def preflight_check(self) -> None:
            return None

        async def send_html(self, text: str, **kwargs: object) -> DeliveryResult:
            return DeliveryResult(status="logged", reason="notifier_disabled")

    delivery = SignalDelivery(LoggedBroadcaster(), pending_expiry_minutes=10)
    signal = Signal(
        symbol="BTCUSDT",
        setup_id="ema_bounce",
        direction="long",
        score=0.8,
        timeframe="15m",
        entry_low=100.0,
        entry_high=101.0,
        stop=98.0,
        take_profit_1=103.0,
        take_profit_2=105.0,
    )

    with caplog.at_level(logging.WARNING, logger="bot.delivery"):
        [result] = await delivery.deliver([signal], dry_run=False)

    assert result.status == "logged"
    assert "signal delivery status is not sent" in caplog.text
    assert "notifier_disabled" in caplog.text


@pytest.mark.asyncio
async def test_signal_bot_startup_delivery_preflight_is_nonfatal(
    caplog: pytest.LogCaptureFixture,
) -> None:
    bot = SignalBot.__new__(SignalBot)
    bot.delivery = SimpleNamespace(
        preflight_check=AsyncMock(side_effect=RuntimeError("telegram preflight failed"))
    )

    with caplog.at_level(logging.WARNING, logger="bot.application.bot"):
        await bot._preflight_delivery_check()

    bot.delivery.preflight_check.assert_awaited_once()
    assert "delivery preflight failed" in caplog.text
