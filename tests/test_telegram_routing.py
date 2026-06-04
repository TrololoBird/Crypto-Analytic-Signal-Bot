"""Tests for Telegram channel vs operator DM routing."""

from __future__ import annotations

from types import SimpleNamespace

from bot.delivery.deliver import format_tracking_event_text
from bot.delivery.telegram_routing import CHANNEL_PURPOSE, OPERATOR_PURPOSE, operator_dm_enabled


def test_operator_dm_enabled_respects_config_flag() -> None:
    bot = SimpleNamespace(
        settings=SimpleNamespace(
            notifiers=SimpleNamespace(
                telegram_operator=SimpleNamespace(enabled=True, send_digest=False)
            )
        )
    )
    assert operator_dm_enabled(bot, "send_digest") is False  # type: ignore[arg-type]
    assert operator_dm_enabled(bot, "send_market_context") is True  # type: ignore[arg-type]


def test_channel_tracking_text_is_subscriber_format() -> None:
    event = SimpleNamespace(
        event_type="stop_loss",
        event_price=1.0,
        occurred_at=None,
        note="",
        tracked=SimpleNamespace(
            symbol="BTCUSDT",
            direction="long",
            tracking_ref="ABC123",
            entry_low=1.0,
            entry_high=1.1,
            stop=0.9,
        ),
    )
    text = format_tracking_event_text(event)  # type: ignore[arg-type]
    assert "POST-MORTEM" not in text
    assert "Operator analytics" not in text


def test_routing_purpose_strings_document_split() -> None:
    assert "канал" in CHANNEL_PURPOSE.lower() or "Канал" in CHANNEL_PURPOSE
    assert "оператор" in OPERATOR_PURPOSE.lower() or "Оператор" in OPERATOR_PURPOSE
