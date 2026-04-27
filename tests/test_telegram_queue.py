from __future__ import annotations

from bot.telegram.queue import MessagePriority, TelegramQueue


def test_telegram_queue_messages_use_timezone_aware_utc_timestamps() -> None:
    queue = TelegramQueue()

    message = queue.enqueue(
        "signal",
        "chat-1",
        priority=MessagePriority.HIGH,
    )

    assert message is not None
    assert message.created_at.tzinfo is not None
    assert message.created_at.utcoffset() is not None
    assert message.created_at.utcoffset().total_seconds() == 0
