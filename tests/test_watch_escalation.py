"""WATCH escalation readiness checks."""

from __future__ import annotations

from bot.domain.config import BotSettings, DeliveryConfig, TrackingConfig
from bot.domain.schemas import Signal
from bot.runtime.watch_escalation import watch_ready_for_action_escalation


def _settings() -> BotSettings:
    return BotSettings(
        tg_token="t",
        target_chat_id="1",
        delivery=DeliveryConfig(action_min_score=0.72, watch_escalation_enabled=True),
        tracking=TrackingConfig(),
    )


def _signal(*, score: float = 0.78) -> Signal:
    entry_low = 100.0
    entry_high = 101.0
    entry_mid = (entry_low + entry_high) / 2.0
    stop = 98.0
    risk = entry_mid - stop
    return Signal(
        symbol="BTCUSDT",
        setup_id="ema_bounce",
        direction="long",
        score=score,
        timeframe="15m",
        entry_low=entry_low,
        entry_high=entry_high,
        stop=stop,
        take_profit_1=entry_mid + risk * 2.0,
        take_profit_2=entry_mid + risk * 2.5,
        risk_reward=2.0,
        mark_price=100.5,
    )


def test_watch_escalation_ready_in_zone() -> None:
    ok, note = watch_ready_for_action_escalation(_signal(), None, settings=_settings())
    assert ok is True
    assert note == "zone_ready"


def test_watch_escalation_rejects_low_score() -> None:
    ok, note = watch_ready_for_action_escalation(_signal(score=0.60), None, settings=_settings())
    assert ok is False
    assert note == "score_below_action"
