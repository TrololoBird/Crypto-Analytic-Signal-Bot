"""Tier cap and session policy tests."""

from __future__ import annotations

from bot.delivery.tiers import decide_with_caps
from bot.domain.config import BotSettings, DeliveryConfig
from bot.domain.schemas import Signal


def _settings(**delivery_overrides: object) -> BotSettings:
    delivery = DeliveryConfig(**delivery_overrides)
    return BotSettings(tg_token="test", target_chat_id="1", delivery=delivery)


def _signal(
    *, symbol: str = "DOGEUSDT", score: float = 0.80, setup_id: str = "ema_bounce"
) -> Signal:
    return Signal(
        symbol=symbol,
        setup_id=setup_id,
        direction="long",
        score=score,
        timeframe="15m",
        entry_low=99.0,
        entry_high=101.0,
        stop=95.0,
        take_profit_1=110.0,
        take_profit_2=115.0,
        risk_reward=2.0,
    )


def test_action_cap_per_cycle_demotes_to_watch_when_slots_available() -> None:
    settings = _settings(action_cap_per_cycle=1, watch_cap_per_cycle=4, action_min_score=0.72)
    signals = [_signal(symbol="BTCUSDT"), _signal(symbol="ETHUSDT")]
    decisions = decide_with_caps(signals, settings)
    allowed_action = [d for d in decisions if d.tier == "action" and d.allow]
    assert len(allowed_action) == 1
    demoted = next(d for d in decisions if d.symbol == "ETHUSDT" and d.tier == "watch" and d.allow)
    assert demoted.reason == "action_cap_demoted_watch"


def test_action_cap_blocks_when_watch_cap_full() -> None:
    settings = _settings(
        action_cap_per_cycle=1,
        watch_cap_per_cycle=1,
        action_min_score=0.72,
        watch_min_score=0.60,
    )
    signals = [
        _signal(symbol="BTCUSDT"),
        _signal(symbol="ETHUSDT"),
        _signal(symbol="SOLUSDT"),
    ]
    decisions = decide_with_caps(signals, settings)
    blocked = next(d for d in decisions if d.symbol == "SOLUSDT" and not d.allow)
    assert blocked.drop_reason == "action_cap_reached"


def test_watch_cap_per_cycle_blocks_excess() -> None:
    settings = _settings(
        action_cap_per_cycle=1,
        watch_cap_per_cycle=2,
        action_min_score=0.95,
        watch_min_score=0.60,
    )
    signals = [
        _signal(symbol="BTCUSDT", score=0.65),
        _signal(symbol="ETHUSDT", score=0.62),
        _signal(symbol="SOLUSDT", score=0.61),
    ]
    decisions = decide_with_caps(signals, settings)
    allowed_watch = [d for d in decisions if d.tier == "watch" and d.allow]
    assert len(allowed_watch) == 2
    blocked = [d for d in decisions if d.drop_reason == "watch_cap_reached"]
    assert len(blocked) == 1
