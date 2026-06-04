"""Delivery alert streak tracking."""

from __future__ import annotations

from types import SimpleNamespace

from bot.domain.config import BotSettings, DeliveryConfig
from bot.runtime.delivery_alerts import (
    _update_zero_delivery_streak,
    delivery_session_snapshot,
    record_cycle_delivery_outcome,
)


def _bot(*, threshold: int = 3) -> SimpleNamespace:
    delivery = DeliveryConfig(zero_delivery_alert_cycles=threshold, action_cap_per_session=10)
    settings = BotSettings(tg_token="t", target_chat_id="1", delivery=delivery)
    return SimpleNamespace(
        settings=settings,
        _zero_delivery_streak=0,
        _last_zero_delivery_alert_mono=0.0,
        _session_action_delivered=4,
        _shortlist=[],
        last_cycle_summary={},
    )


def test_zero_streak_resets_on_delivery() -> None:
    bot = _bot()
    bot._zero_delivery_streak = 5
    streak = _update_zero_delivery_streak(bot, delivered_count=1)  # type: ignore[arg-type]
    assert streak == 0
    assert bot._zero_delivery_streak == 0


def test_zero_streak_increments_without_delivery() -> None:
    bot = _bot()
    streak = _update_zero_delivery_streak(bot, delivered_count=0)  # type: ignore[arg-type]
    assert streak == 1
    assert bot._zero_delivery_streak == 1


def test_record_cycle_outcome_no_loop_is_safe() -> None:
    bot = _bot()
    record_cycle_delivery_outcome(bot, delivered_count=0)  # type: ignore[arg-type]
    assert bot._zero_delivery_streak == 1


def test_session_snapshot_remaining() -> None:
    bot = _bot()
    snap = delivery_session_snapshot(bot)  # type: ignore[arg-type]
    assert snap["session_action_delivered"] == 4
    assert snap["session_action_remaining"] == 6
