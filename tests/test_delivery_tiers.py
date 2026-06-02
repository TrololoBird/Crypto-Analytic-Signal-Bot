"""Unit tests for WATCH/ACTION tier classification (no network)."""

from __future__ import annotations

from bot.delivery.tiers import classify_tier
from bot.domain.config import BotSettings, DeliveryConfig
from bot.domain.delivery_policy import effective_action_min_score
from bot.domain.schemas import Signal


def _settings(**delivery_overrides: object) -> BotSettings:
    delivery = DeliveryConfig(**delivery_overrides)
    return BotSettings(tg_token="test", target_chat_id="1", delivery=delivery)


def _signal(*, symbol: str = "DOGEUSDT", score: float = 0.74) -> Signal:
    return Signal(
        symbol=symbol,
        setup_id="ema_bounce",
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


def test_alt_action_at_base_threshold() -> None:
    settings = _settings(action_min_score=0.72, anchor_action_score_delta=0.04)
    decision = classify_tier(_signal(symbol="DOGEUSDT", score=0.73), settings)
    assert decision.tier == "action"
    assert decision.reason == "score_action"


def test_anchor_requires_higher_score_for_action() -> None:
    settings = _settings(action_min_score=0.72, anchor_action_score_delta=0.04)
    borderline = classify_tier(_signal(symbol="BTCUSDT", score=0.73), settings)
    assert borderline.tier == "watch"
    assert borderline.reason == "score_watch"

    action = classify_tier(_signal(symbol="BTCUSDT", score=0.76), settings)
    assert action.tier == "action"
    assert action.reason == "score_action_anchor"


def test_metal_anchor_adds_extra_delta() -> None:
    settings = _settings(
        action_min_score=0.72,
        anchor_action_score_delta=0.04,
        metal_action_score_delta=0.02,
    )
    assert effective_action_min_score(settings, "XAUUSDT") == 0.78
    assert classify_tier(_signal(symbol="XAUUSDT", score=0.77), settings).tier == "watch"
    assert classify_tier(_signal(symbol="XAUUSDT", score=0.79), settings).tier == "action"
