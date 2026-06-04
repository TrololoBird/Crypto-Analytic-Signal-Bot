"""Wave F10 Agent S — delivery messaging: escalation, routing, webhook, confluence, rank."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import polars as pl
import pytest

from bot.delivery.ops_webhook import (
    _plain_text,
    notify_ops_delivery_failed,
    notify_ops_tier_cap_starvation,
    send_ops_webhook_alert,
)
from bot.delivery.telegram_routing import (
    send_operator_analytics_companion,
    should_send_channel_analytics_companion,
)
from bot.delivery.tiers import decide_with_caps, rank_key
from bot.domain.config import (
    BotSettings,
    DeliveryConfig,
    NotifierConfig,
    NotifierWebhookConfig,
    TelegramOperatorConfig,
)
from bot.domain.schemas import Signal
from bot.runtime.delivery_orchestrator import DeliveryOrchestrator
from bot.runtime.watch_escalation import maybe_notify_watch_escalation


def _settings(**overrides: object) -> BotSettings:
    base: dict[str, object] = {"tg_token": "test", "target_chat_id": "1"}
    base.update(overrides)
    return BotSettings(**base)


def _signal(**overrides: object) -> Signal:
    entry_low = 100.0
    entry_high = 101.0
    entry_mid = (entry_low + entry_high) / 2.0
    stop = 98.0
    risk = entry_mid - stop
    base = dict(
        symbol="BTCUSDT",
        setup_id="ema_bounce",
        direction="long",
        score=0.78,
        timeframe="15m",
        entry_low=entry_low,
        entry_high=entry_high,
        stop=stop,
        take_profit_1=entry_mid + risk * 2.0,
        take_profit_2=entry_mid + risk * 2.5,
        risk_reward=2.0,
        mark_price=100.5,
    )
    base.update(overrides)
    return Signal(**base)  # type: ignore[arg-type]


def _bot_stub(**overrides: object) -> SimpleNamespace:
    settings = _settings(**overrides)
    return SimpleNamespace(
        settings=settings,
        _watch_escalation_states={},
        _ops_webhook_session=None,
    )


# --- S5: watch escalation state change + send_watch_escalation + ops mirror ---


@pytest.mark.asyncio
async def test_watch_escalation_notifies_only_on_state_change() -> None:
    bot = _bot_stub(
        notifiers=NotifierConfig(
            telegram_operator=TelegramOperatorConfig(send_watch_escalation=True),
        ),
    )
    signal = _signal()
    send_html = AsyncMock(return_value=1)
    webhook = AsyncMock(return_value=False)

    with (
        patch("bot.delivery.telegram_routing.send_operator_html", send_html),
        patch("bot.delivery.ops_webhook.send_ops_webhook_alert", webhook),
    ):
        await maybe_notify_watch_escalation(bot, signal, None)  # type: ignore[arg-type]
        await maybe_notify_watch_escalation(bot, signal, None)  # type: ignore[arg-type]

    assert send_html.await_count == 1
    assert webhook.await_count == 1
    assert bot._watch_escalation_states[signal.tracking_id] == "zone_ready"


@pytest.mark.asyncio
async def test_watch_escalation_respects_send_watch_escalation_flag() -> None:
    bot = _bot_stub(
        notifiers=NotifierConfig(
            telegram_operator=TelegramOperatorConfig(send_watch_escalation=False),
        ),
    )
    signal = _signal()
    send_html = AsyncMock(return_value=1)

    with patch("bot.delivery.telegram_routing.send_operator_html", send_html):
        await maybe_notify_watch_escalation(bot, signal, None)  # type: ignore[arg-type]

    send_html.assert_not_awaited()


# --- S6: telegram routing companion ---


def test_should_send_channel_companion_action_only() -> None:
    cfg = NotifierConfig(send_analytics_companion=True, analytics_companion_action_only=True)
    assert should_send_channel_analytics_companion(cfg, tier="action") is True
    assert should_send_channel_analytics_companion(cfg, tier="watch") is False


def test_should_send_channel_companion_all_tiers_by_default() -> None:
    cfg = NotifierConfig(send_analytics_companion=True)
    assert should_send_channel_analytics_companion(cfg, tier="watch") is True


@pytest.mark.asyncio
async def test_send_operator_analytics_companion_uses_operator_dm() -> None:
    bot = _bot_stub(
        notifiers=NotifierConfig(
            telegram_operator=TelegramOperatorConfig(send_watch_companion=True),
        ),
    )
    signal = _signal()
    send_html = AsyncMock(return_value=1)

    with patch("bot.delivery.telegram_routing.send_operator_html", send_html):
        sent = await send_operator_analytics_companion(
            bot,  # type: ignore[arg-type]
            signal,
            btc_bias="bear",
            eth_bias="neutral",
        )

    assert sent == 1
    send_html.assert_awaited_once()
    assert "WHY THIS SIGNAL" in send_html.await_args.args[1]


# --- S7: ops webhook html.unescape + events + session reuse ---


def test_plain_text_unescapes_html_entities() -> None:
    assert _plain_text("<b>alert</b> &amp; ok") == "alert & ok"


@pytest.mark.asyncio
async def test_send_ops_webhook_reuses_bot_session() -> None:
    bot = _bot_stub(
        notifiers=NotifierConfig(
            webhook=NotifierWebhookConfig(
                enabled=True,
                webhook_url="https://example.com/hook",
                ops_alerts_enabled=True,
            ),
        ),
    )
    mock_resp = AsyncMock()
    mock_resp.status = 200
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)

    mock_session = MagicMock()
    mock_session.closed = False
    mock_session.post = MagicMock(return_value=mock_resp)
    bot._ops_webhook_session = mock_session

    ok = await send_ops_webhook_alert(
        bot,  # type: ignore[arg-type]
        event="critical_error",
        text="<b>test</b>",
    )
    assert ok is True
    assert mock_session.post.call_count == 1
    await send_ops_webhook_alert(
        bot,  # type: ignore[arg-type]
        event="critical_error",
        text="<b>again</b>",
    )
    assert mock_session.post.call_count == 2


@pytest.mark.asyncio
async def test_notify_ops_delivery_failed_event() -> None:
    bot = _bot_stub(
        notifiers=NotifierConfig(
            webhook=NotifierWebhookConfig(
                enabled=True,
                webhook_url="https://example.com/hook",
                ops_alerts_enabled=True,
            ),
        ),
    )
    mock_resp = AsyncMock()
    mock_resp.status = 200
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)
    mock_session = MagicMock()
    mock_session.closed = False
    mock_session.post = MagicMock(return_value=mock_resp)
    bot._ops_webhook_session = mock_session

    ok = await notify_ops_delivery_failed(
        bot,  # type: ignore[arg-type]
        symbol="BTCUSDT",
        setup_id="ema_bounce",
        direction="long",
        reason="delivery_failed",
        delivery_reason="telegram_timeout",
    )
    assert ok is True
    payload = mock_session.post.call_args.kwargs["json"]
    assert payload["event"] == "delivery_failed"
    assert "Delivery failed" in payload["text"]


@pytest.mark.asyncio
async def test_notify_ops_tier_cap_starvation_event() -> None:
    bot = _bot_stub(
        notifiers=NotifierConfig(
            webhook=NotifierWebhookConfig(
                enabled=True,
                webhook_url="https://example.com/hook",
                ops_alerts_enabled=True,
            ),
        ),
    )
    mock_resp = AsyncMock()
    mock_resp.status = 200
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)
    mock_session = MagicMock()
    mock_session.closed = False
    mock_session.post = MagicMock(return_value=mock_resp)
    bot._ops_webhook_session = mock_session

    ok = await notify_ops_tier_cap_starvation(
        bot,  # type: ignore[arg-type]
        symbol="ETHUSDT",
        setup_id="order_block",
        direction="short",
        tier="action",
        drop_reason="action_cap_reached",
    )
    assert ok is True
    payload = mock_session.post.call_args.kwargs["json"]
    assert payload["event"] == "tier_cap_starvation"


# --- S8: weighted confluence bridge uses ConfluenceEngine ---


def _prepared_gate() -> SimpleNamespace:
    rows = 25
    frame = pl.DataFrame(
        {
            "close": [95.0] * rows,
            "ema20": [100.0] * rows,
            "ema50": [105.0] * rows,
            "rsi14": [70.0] * rows,
            "volume": [80.0] * (rows - 1) + [150.0],
            "high": [96.0] * rows,
            "low": [94.0] * rows,
            "open": [95.0] * rows,
            "atr14": [1.2] * rows,
            "volume_ratio20": [1.1] * rows,
        }
    )
    h1 = pl.DataFrame({"close": [120.0 + i for i in range(60)], "high": [121.0] * 60, "low": [119.0] * 60, "open": [120.0] * 60})
    return SimpleNamespace(
        work_15m=frame,
        work_1h=h1,
        work_4h=h1,
        regime_1h_confirmed="ranging",
        regime_4h_confirmed="ranging",
        microprice_bias=0.08,
        agg_trade_delta_30s=0.02,
        funding_rate=0.0001,
        oi_change_pct=1.0,
        universe=SimpleNamespace(price_change_pct=1.0, quote_volume=1e9),
        settings=_settings(),
    )


def test_use_weighted_confluence_runs_confluence_engine() -> None:
    prepared = _prepared_gate()
    signal = _signal(score=0.80, confirmation_profile="trend_follow")
    settings = _settings(delivery=DeliveryConfig(use_weighted_confluence=True))
    mock_engine = MagicMock()
    mock_engine.score.return_value = SimpleNamespace(
        final_score=0.80,
        to_dict=lambda: {"final_score": 0.80, "weighted_model_score": 0.65},
    )
    _, _, details = DeliveryOrchestrator._hard_confluence_gate(
        signal,
        prepared,  # type: ignore[arg-type]
        enforce_mtf_gate=False,
        use_weighted_confluence=True,
        settings=settings,
        confluence_engine=mock_engine,
    )
    assert mock_engine.score.called
    assert details.get("weighted_confluence_bridge") is True
    assert details.get("confluence_engine") == {"final_score": 0.80, "weighted_model_score": 0.65}


def test_use_weighted_confluence_bridge_can_pass_near_threshold() -> None:
    prepared = _prepared_gate()
    signal = _signal(score=0.80, confirmation_profile="trend_follow")
    settings = _settings(delivery=DeliveryConfig(use_weighted_confluence=True, action_min_score=0.72))
    mock_engine = MagicMock()
    mock_engine.score.return_value = SimpleNamespace(
        final_score=0.85,
        to_dict=lambda: {"final_score": 0.85, "weighted_model_score": 0.7},
    )
    ok, _, details = DeliveryOrchestrator._hard_confluence_gate(
        signal,
        prepared,  # type: ignore[arg-type]
        enforce_mtf_gate=False,
        use_weighted_confluence=True,
        settings=settings,
        confluence_engine=mock_engine,
    )
    assert mock_engine.score.called
    assert details.get("weighted_confluence_pass") is True


# --- S10: rank_key includes confirmation_count ---


def test_rank_key_prefers_higher_confirmation_count() -> None:
    high = _signal(score=0.75, confirmation_count=4)
    low = _signal(score=0.75, confirmation_count=2)
    assert rank_key(high) > rank_key(low)


def test_tier_cap_uses_confirmation_count_in_ordering() -> None:
    settings = _settings(
        delivery=DeliveryConfig(
            action_cap_per_cycle=1,
            watch_cap_per_cycle=2,
            action_min_score=0.72,
            watch_min_score=0.60,
        ),
    )
    first = _signal(symbol="BTCUSDT", score=0.80, confirmation_count=2)
    second = _signal(symbol="ETHUSDT", score=0.80, confirmation_count=5)
    decisions = decide_with_caps([first, second], settings)
    eth = next(row for row in decisions if row.symbol == "ETHUSDT")
    btc = next(row for row in decisions if row.symbol == "BTCUSDT")
    assert eth.tier == "action"
    assert btc.tier == "watch"
    assert btc.reason == "action_cap_demoted_watch"
