"""Wave F9 agent S — Telegram validation, RR display, tier badges, cap demotion."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from bot.delivery.deliver import SignalDelivery, format_signal_text
from bot.delivery.formatting import (
    SignalMessageFacts,
    extract_signal_facts,
    format_channel_trade_card,
    format_safe_signal_fallback,
    validate_telegram_html,
)
from bot.delivery.tiers import decide_with_caps
from bot.domain.config import BotSettings, DeliveryConfig
from bot.domain.schemas import Signal


def _facts(**overrides: object) -> SignalMessageFacts:
    base = dict(
        symbol="BTCUSDT",
        direction="long",
        setup_id="ema_bounce",
        timeframe="15m",
        tracking_ref="A1",
        score=0.72,
        entry_low=100.0,
        entry_high=101.0,
        stop=98.0,
        take_profit_1=104.0,
        take_profit_2=106.0,
        take_profit_3=108.0,
        risk_reward=2.0,
        stop_distance_pct=2.0,
        valid_until=None,
    )
    base.update(overrides)
    return SignalMessageFacts(**base)  # type: ignore[arg-type]


def _signal(**overrides: object) -> Signal:
    base = dict(
        symbol="BTCUSDT",
        setup_id="ema_bounce",
        direction="long",
        score=0.80,
        timeframe="15m",
        entry_low=99.0,
        entry_high=101.0,
        stop=95.0,
        take_profit_1=110.0,
        take_profit_2=115.0,
        take_profit_3=120.0,
        risk_reward=2.0,
    )
    base.update(overrides)
    return Signal(**base)  # type: ignore[arg-type]


def test_channel_card_shows_rr1_rr2_rr3_from_trade_plan_fields() -> None:
    facts = _facts(
        risk_reward_tp1=1.5,
        risk_reward_tp2=3.0,
        risk_reward_tp3=5.0,
    )
    card = format_channel_trade_card(facts, include_chart=False)
    assert "RR1" in card
    assert "RR2" in card
    assert "RR3" in card
    assert "1.5" in card
    assert "3.0" in card
    assert "5.0" in card


def test_channel_card_shows_weighted_rr_for_single_target_mode() -> None:
    facts = _facts(
        take_profit_2=104.0,
        take_profit_3=104.0,
        risk_reward_tp1=2.0,
        risk_reward_tp2=2.0,
        risk_reward_tp3=2.0,
        weighted_risk_reward=2.0,
    )
    card = format_channel_trade_card(facts, include_chart=False)
    assert "RR1" not in card
    assert "RR 2.0" in card or "RR <code>2.0</code>" in card


def test_extract_signal_facts_computes_rr_from_prices() -> None:
    signal = SimpleNamespace(
        symbol="BTCUSDT",
        direction="long",
        setup_id="ema_bounce",
        timeframe="15m",
        tracking_ref="X1",
        score=0.7,
        entry_low=100.0,
        entry_high=100.0,
        stop=98.0,
        take_profit_1=104.0,
        take_profit_2=106.0,
        take_profit_3=108.0,
        risk_reward=3.0,
        stop_distance_pct=2.0,
        scale_weights=(0.5, 0.3, 0.2),
        created_at=datetime.now(UTC),
    )
    facts = extract_signal_facts(signal, pending_expiry_minutes=180)
    assert facts.risk_reward_tp1 == pytest.approx(2.0)
    assert facts.risk_reward_tp2 == pytest.approx(3.0)
    assert facts.risk_reward_tp3 == pytest.approx(4.0)
    assert facts.weighted_risk_reward == pytest.approx(2.0 * 0.5 + 3.0 * 0.3 + 4.0 * 0.2)


def test_channel_card_watch_and_action_badges() -> None:
    facts = _facts()
    action_card = format_channel_trade_card(facts, include_chart=False, tier="action")
    watch_card = format_channel_trade_card(facts, include_chart=False, tier="watch")
    assert "[ACTION]" in action_card
    assert "[WATCH]" in watch_card


def test_format_signal_text_passes_tier_to_card() -> None:
    signal = SimpleNamespace(
        symbol="ETHUSDT",
        direction="short",
        setup_id="order_block",
        timeframe="15m",
        tracking_ref="W1",
        score=0.68,
        entry_low=200.0,
        entry_high=201.0,
        stop=205.0,
        take_profit_1=195.0,
        take_profit_2=190.0,
        take_profit_3=185.0,
        risk_reward=2.0,
        stop_distance_pct=2.0,
        created_at=datetime.now(UTC),
    )
    text = format_signal_text(signal, pending_expiry_minutes=180, tier="watch")  # type: ignore[arg-type]
    assert "[WATCH]" in text


def test_safe_fallback_is_valid_telegram_html() -> None:
    signal = _signal()
    text = format_safe_signal_fallback(signal, pending_expiry_minutes=180, tier="action")
    report = validate_telegram_html(text)
    assert report.ok is True
    assert "BTCUSDT" in text


@pytest.mark.asyncio
async def test_deliver_uses_safe_fallback_on_invalid_html(monkeypatch: pytest.MonkeyPatch) -> None:
    signal = _signal()
    broadcaster = SimpleNamespace(
        preflight_check=AsyncMock(),
        send_html=AsyncMock(return_value=SimpleNamespace(status="sent", message_id=42, reason=None)),
        edit_html=AsyncMock(),
        close=AsyncMock(),
    )
    delivery = SignalDelivery(broadcaster, pending_expiry_minutes=180)

    def _bad_format(*_args: object, **_kwargs: object) -> str:
        return "<script>bad</script>"

    monkeypatch.setattr("bot.delivery.deliver.format_signal_text", _bad_format)

    results = await delivery.deliver([signal], dry_run=False, tier_by_tracking_id={signal.tracking_id: "action"})
    assert results[0].status == "sent"
    sent_text = broadcaster.send_html.await_args.args[0]
    assert validate_telegram_html(sent_text).ok is True
    assert "[ACTION]" in sent_text


def test_action_cap_demotes_to_watch_when_watch_slots_available() -> None:
    settings = BotSettings(
        tg_token="test",
        target_chat_id="1",
        delivery=DeliveryConfig(
            action_cap_per_cycle=1,
            watch_cap_per_cycle=3,
            action_min_score=0.72,
            watch_min_score=0.60,
        ),
    )
    signals = [_signal(symbol="BTCUSDT"), _signal(symbol="ETHUSDT")]
    decisions = decide_with_caps(signals, settings)
    eth = next(d for d in decisions if d.symbol == "ETHUSDT")
    assert eth.allow is True
    assert eth.tier == "watch"
    assert eth.reason == "action_cap_demoted_watch"
