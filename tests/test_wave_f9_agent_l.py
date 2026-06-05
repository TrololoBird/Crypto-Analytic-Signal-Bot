"""Wave F9 agent L: runtime orchestration bootstrap, funnel noise, score floor, cycle timeout."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bot.domain.config import BotSettings, FilterConfig, RuntimeConfig
from bot.domain.schemas import PipelineResult, Signal
from bot.domain.strategies import StrategyDecision
from bot.runtime.bot import SignalBot
from bot.runtime.container import build_application_container
from bot.runtime.cycle_runner import CycleRunner
from bot.runtime.telemetry_manager import TelemetryManager

_TEST_TOKEN = "123456789:ABCdefGHIjklMNOpqrsTUVwxyz"


def test_emit_strategy_routing_skips_defaults_false() -> None:
    assert RuntimeConfig().emit_strategy_routing_skips is False


def test_select_and_rank_uses_filters_min_score() -> None:
    bot = MagicMock()
    bot.settings = BotSettings(
        tg_token=_TEST_TOKEN,
        target_chat_id="1",
        filters=FilterConfig(min_score=0.61),
    )
    low = Signal(
        symbol="BTCUSDT",
        setup_id="test",
        direction="long",
        score=0.60,
        timeframe="15m",
        entry_low=99.0,
        entry_high=101.0,
        stop=98.0,
        take_profit_1=103.0,
        take_profit_2=105.0,
    )
    high = Signal(
        symbol="BTCUSDT",
        setup_id="test",
        direction="long",
        score=0.62,
        timeframe="15m",
        entry_low=99.0,
        entry_high=101.0,
        stop=98.0,
        take_profit_1=103.0,
        take_profit_2=105.0,
    )
    bot._delivery_orchestrator.select_and_rank.return_value = [low, high]

    ranked = SignalBot._select_and_rank(bot, {"BTCUSDT": [low, high]}, max_signals=2)
    assert [signal.score for signal in ranked] == [0.62]


def test_lane_skips_aggregate_in_rejection_stats() -> None:
    bot = MagicMock()
    bot.settings = BotSettings(tg_token=_TEST_TOKEN, target_chat_id="1")
    bot.telemetry = MagicMock()
    manager = TelemetryManager(bot)
    decision = StrategyDecision.skip(
        setup_id="ema_bounce",
        reason_code="runtime.strategy_lane_excluded",
        details={"symbol": "BTCUSDT"},
    )
    manager.append_strategy_decision(symbol="BTCUSDT", trigger="kline", decision=decision)
    assert manager._lane_skip_count == 1
    assert "ema_bounce:runtime.strategy_lane_excluded" not in manager._rejection_counts


def test_emit_cycle_log_aggregates_lane_skips_in_funnel() -> None:
    bot = MagicMock()
    bot.settings = BotSettings(tg_token=_TEST_TOKEN, target_chat_id="1")
    bot.telemetry = MagicMock()
    bot._shortlist_source = "live"
    bot._prepare_error_count = 0
    bot._ws_manager = None
    bot._bus = SimpleNamespace(stats=dict)
    bot.client = SimpleNamespace(state_snapshot=dict)
    bot.last_cycle_summary = {}
    bot.dashboard = None

    manager = TelemetryManager(bot)
    lane_row = {
        "setup_id": "ema_bounce",
        "reason": "runtime.strategy_lane_excluded",
        "reason_code": "runtime.strategy_lane_excluded",
    }
    other_row = {"setup_id": "rsi_div", "reason": "pattern.no_hit", "reason_code": "pattern.no_hit"}
    result = PipelineResult(
        symbol="BTCUSDT",
        trigger="kline_close",
        event_ts=datetime.now(UTC),
        raw_setups=12,
        funnel={"detector_runs": 12},
    )
    manager.emit_cycle_log(
        symbol="BTCUSDT",
        interval="15m",
        event_ts=result.event_ts,
        shortlist_size=40,
        tracking_events=[],
        result=result,
        candidates=[],
        rejected=[lane_row, lane_row, other_row],
        delivered=[],
    )

    cycle_row = bot.telemetry.append_jsonl.call_args_list[0].args[1]
    assert cycle_row["lane_skip_count"] == 2
    assert cycle_row["rejected_count"] == 1
    assert cycle_row["funnel"]["lane_skips_aggregated"]["count"] == 2


@pytest.mark.asyncio
async def test_build_application_container_defers_network_bootstrap_in_async_context() -> None:
    settings = BotSettings(tg_token=_TEST_TOKEN, target_chat_id="1")
    ensure = AsyncMock(return_value=settings)

    with (
        patch("bot.runtime.container.ensure_network_ready", ensure),
        patch("bot.runtime.container.build_message_broadcaster", return_value=MagicMock()),
    ):
        container = build_application_container(
            settings,
            register_strategies=lambda _registry: None,
        )

    ensure.assert_not_awaited()
    assert container.client is not None


@pytest.mark.asyncio
async def test_signal_bot_start_runs_network_bootstrap() -> None:
    settings = BotSettings(tg_token=_TEST_TOKEN, target_chat_id="1")
    ensure = AsyncMock(return_value=settings)

    bot = MagicMock()
    bot.settings = settings
    bot._preflight_storage_check = MagicMock()
    bot._preflight_delivery_check = AsyncMock()
    bot._modern_repo = AsyncMock()
    bot._modern_repo.initialize = AsyncMock()
    bot._modern_repo.expire_open_signals_older_than = AsyncMock(return_value=0)
    bot._modern_repo.purge_cooldowns_older_than = AsyncMock(return_value=0)
    bot._modern_repo.get_active_signals = AsyncMock(return_value=[])
    bot._modern_repo.summary = AsyncMock(return_value={"symbol_count": 0})
    bot._modern_repo.get_market_context = AsyncMock(return_value={"btc_bias": "neutral"})
    bot.tracker = AsyncMock()
    bot.tracker.repair_stuck_pending_activations = AsyncMock(return_value=[])
    bot.tracker.review_open_signals = AsyncMock(return_value=[])
    bot.tracker.reconcile_closed_outcomes = AsyncMock(return_value=0)
    bot._sync_ws_tracked_symbols = AsyncMock()
    bot._ws_manager = None
    bot._dashboard_enabled = False
    bot._http_servers_enabled = False
    bot._ensure_dashboard_started = AsyncMock()
    bot.client = object()
    bot._background_tasks = set()
    bot._running = False

    with (
        patch("bot.runtime.bot.ensure_network_ready", ensure),
        patch("bot.diagnostics.config_audit.run_startup_audit"),
    ):
        await SignalBot.start(bot)

    ensure.assert_awaited_once()
    bot._preflight_storage_check.assert_called_once()


@pytest.mark.asyncio
async def test_cycle_timeout_emits_reject_row_and_cycle_log() -> None:
    bot = MagicMock()
    bot.settings = BotSettings(
        tg_token=_TEST_TOKEN,
        target_chat_id="1",
        runtime=RuntimeConfig(cycle_timeout_seconds=30.0),
    )
    bot._analysis_semaphore = asyncio.Semaphore(1)
    bot.telemetry = MagicMock()
    bot._emit_cycle_log = MagicMock()

    runner = CycleRunner(bot)
    item = SimpleNamespace(symbol="ETHUSDT")
    event_ts = datetime.now(UTC)

    with patch.object(asyncio, "wait_for", side_effect=TimeoutError):
        await runner.execute_symbol_cycle(
            symbol="ETHUSDT",
            item=item,
            interval="15m",
            trigger="kline_close",
            event_ts=event_ts,
            tracking_events=[],
            shortlist_size=10,
        )

    bot.telemetry.append_jsonl.assert_called_once()
    reject_call = bot.telemetry.append_jsonl.call_args
    assert reject_call.args[0] == "rejected.jsonl"
    reject_row = reject_call.args[1]
    assert reject_row["reason_code"] == "runtime.cycle_timeout"
    assert reject_row["timeout_seconds"] == 30.0

    bot._emit_cycle_log.assert_called_once()
    emit_kwargs = bot._emit_cycle_log.call_args.kwargs
    assert emit_kwargs["symbol"] == "ETHUSDT"
    assert emit_kwargs["result"].status == "cycle_timeout"
    assert emit_kwargs["rejected"][0]["reason_code"] == "runtime.cycle_timeout"
