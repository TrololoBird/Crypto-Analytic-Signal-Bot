"""Channel lifecycle messages for manual limit-entry tracking."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

from bot.delivery.deliver import SignalDelivery
from bot.delivery.formatting import format_signal_message, format_tracking_event_message
from bot.persistence.tracking import SignalTrackingEvent


def _event(event_type: str, *, note: str = "") -> SignalTrackingEvent:
    tracked = SimpleNamespace(
        symbol="BTCUSDT",
        direction="long",
        tracking_ref="TREF1",
        setup_id="ema_bounce",
        timeframe="15m",
        entry_low=100.0,
        entry_high=101.0,
        stop=98.0,
        take_profit_1=104.0,
        take_profit_2=106.0,
        take_profit_3=108.0,
        risk_reward=2.0,
        stop_distance_pct=2.0,
        score=0.72,
        activated_at=None,
        signal_message_id=123,
    )
    return SignalTrackingEvent(
        event_type=event_type,
        tracked=tracked,  # type: ignore[arg-type]
        occurred_at=datetime.now(UTC),
        event_price=100.5,
        precision_mode="candle",
        note=note,
    )


def test_format_signal_message_compact_russian() -> None:
    signal = SimpleNamespace(
        symbol="BTCUSDT",
        direction="long",
        setup_id="ema_bounce",
        timeframe="15m",
        tracking_ref="TREF1",
        score=0.72,
        entry_low=100.0,
        entry_high=101.0,
        stop=98.0,
        take_profit_1=104.0,
        take_profit_2=106.0,
        take_profit_3=108.0,
        risk_reward=2.0,
        stop_distance_pct=2.0,
        created_at=datetime.now(UTC),
    )
    text = format_signal_message(signal, pending_expiry_minutes=180)
    assert "LONG" in text
    assert "BTCUSDT" in text
    assert "SIGNAL-ONLY PLAN" not in text
    assert "Invalidation" not in text
    assert "Вход" in text


def test_format_activated_short_reply() -> None:
    text = format_tracking_event_message(_event("activated", note="limit_filled"))
    assert "В СДЕЛКЕ" in text
    assert "BTCUSDT" in text


def test_format_activated_sent_to_channel_policy() -> None:
    delivery = SignalDelivery(SimpleNamespace(), pending_expiry_minutes=180)
    event = _event("activated", note="limit_filled")
    assert delivery._should_send_tracking_follow_up(event) is True


def test_setup_invalidated_not_channel_follow_up() -> None:
    delivery = SignalDelivery(SimpleNamespace(), pending_expiry_minutes=180)
    event = _event("setup_invalidated", note="stop_before_limit_fill")
    assert delivery._should_send_tracking_follow_up(event) is False


def test_expired_pending_is_channel_follow_up() -> None:
    delivery = SignalDelivery(SimpleNamespace(), pending_expiry_minutes=180)
    event = _event("expired", note="pending_expired")
    assert delivery._should_send_tracking_follow_up(event) is True
