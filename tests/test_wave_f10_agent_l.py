"""Wave F10 Agent L: intra-candle fast lane, cycle context, semaphore, merge, family gates."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bot.domain.config import BotSettings, TrackingConfig, WSConfig
from bot.domain.schemas import PipelineResult, Signal
from bot.engine.engine import SignalEngine
from bot.runtime.analyzer.family_gates import AnalyzerFamilyGatesMixin
from bot.runtime.cycle_runner import CycleContext, CycleRunner
from bot.runtime.delivery_orchestrator import DeliveryOrchestrator
from bot.runtime.merge import MetaSignalMerger
from bot.runtime.telemetry_manager import TelemetryManager

_TEST_TOKEN = "123456789:ABCdefGHIjklMNOpqrsTUVwxyz"


def _signal(
    *,
    direction: str = "long",
    score: float = 0.8,
    setup_id: str = "ema_bounce",
    created_at: datetime | None = None,
) -> Signal:
    if direction == "short":
        entry_low = 3420.0
        entry_high = 3438.0
        entry_mid = (entry_low + entry_high) / 2.0
        stop = 3465.0
        risk = stop - entry_mid
        take_profit_1 = entry_mid - risk * 2.0
        take_profit_2 = entry_mid - risk * 2.5
    else:
        entry_low = 3420.0
        entry_high = 3438.0
        entry_mid = (entry_low + entry_high) / 2.0
        stop = 3395.0
        risk = entry_mid - stop
        take_profit_1 = entry_mid + risk * 2.0
        take_profit_2 = entry_mid + risk * 2.5

    kwargs: dict[str, object] = {
        "symbol": "BTCUSDT",
        "setup_id": setup_id,
        "direction": direction,
        "score": score,
        "timeframe": "15m",
        "entry_low": entry_low,
        "entry_high": entry_high,
        "stop": stop,
        "take_profit_1": take_profit_1,
        "take_profit_2": take_profit_2,
        "risk_reward": 2.0,
    }
    if created_at is not None:
        kwargs["created_at"] = created_at
        kwargs["valid_until"] = created_at + timedelta(days=30)
    return Signal(**kwargs)


def test_l6_intra_candle_max_setups_config_default() -> None:
    assert WSConfig().intra_candle_max_setups == 8
    assert WSConfig().intra_candle_setup_subset == ()


def test_l6_intra_candle_detector_limits_from_config() -> None:
    bot = MagicMock()
    bot.settings = BotSettings(
        tg_token=_TEST_TOKEN,
        target_chat_id="1",
        ws=WSConfig(
            intra_candle_max_setups=5,
            intra_candle_setup_subset=("price_velocity", "absorption"),
        ),
    )
    max_setups, subset = CycleRunner._intra_candle_detector_limits(bot)
    assert max_setups == 5
    assert subset == frozenset({"price_velocity", "absorption"})


def test_l6_engine_apply_detector_limits() -> None:
    engine = SignalEngine(registry=MagicMock(), settings=MagicMock())
    strategies = [
        SimpleNamespace(strategy_id="a"),
        SimpleNamespace(strategy_id="b"),
        SimpleNamespace(strategy_id="c"),
        SimpleNamespace(strategy_id="d"),
    ]
    limited = engine._apply_detector_limits(
        strategies,
        max_setups=2,
        setup_subset=frozenset({"a", "c", "d"}),
    )
    assert [item.strategy_id for item in limited] == ["a", "c"]


def test_l8_prepare_cycle_context_shared_by_normal_and_emergency() -> None:
    bot = MagicMock()
    bot.settings.runtime.emergency_context_fetch_timeout_seconds = 5.0
    bot._fetch_frames = AsyncMock(return_value=SimpleNamespace(symbol="ETHUSDT"))
    bot._ws_cache_enrichments = MagicMock(return_value={"funding_rate": 0.0001})
    bot._spot_enrichments = MagicMock(return_value={"basis_pct": 0.01})
    bot.telemetry = MagicMock()
    bot._get_oi_refresh_runner = MagicMock()

    runner = CycleRunner(bot)
    item = SimpleNamespace(symbol="ETHUSDT")

    with patch(
        "bot.runtime.cycle_runner.missing_derivatives_context",
        return_value=[],
    ):
        context = asyncio.run(
            runner._prepare_cycle_context(
                item=item,
                symbol="ETHUSDT",
                include_spot_enrichments=True,
                require_derivatives=True,
            )
        )

    assert isinstance(context, CycleContext)
    assert context.ws_enrichments["basis_pct"] == 0.01
    bot._spot_enrichments.assert_called_once()


@pytest.mark.asyncio
async def test_l9_analysis_semaphore_covers_select_and_deliver() -> None:
    bot = MagicMock()
    bot.settings = BotSettings(tg_token=_TEST_TOKEN, target_chat_id="1")
    bot.telemetry = MagicMock()
    bot._emit_cycle_log = MagicMock()

    sem = asyncio.Semaphore(1)
    bot._analysis_semaphore = sem
    pipeline_result = PipelineResult(
        symbol="BTCUSDT",
        trigger="kline_close",
        event_ts=datetime.now(UTC),
        raw_setups=3,
        candidates=[_signal()],
    )
    bot._run_modern_analysis = AsyncMock(return_value=pipeline_result)
    bot._select_and_deliver_for_symbol = AsyncMock(return_value=([], [], []))

    runner = CycleRunner(bot)
    context = CycleContext(
        frames=SimpleNamespace(symbol="BTCUSDT"),
        ws_enrichments={},
    )
    with patch.object(runner, "_prepare_cycle_context", AsyncMock(return_value=context)):
        await runner._execute_symbol_cycle_unbounded(
            symbol="BTCUSDT",
            item=SimpleNamespace(symbol="BTCUSDT"),
            interval="15m",
            trigger="kline_close",
            event_ts=datetime.now(UTC),
            tracking_events=[],
            shortlist_size=10,
        )

    assert sem.locked() is False
    bot._select_and_deliver_for_symbol.assert_awaited_once()


def test_l10_merger_uses_tracking_action_window_hours() -> None:
    settings = BotSettings(
        tg_token=_TEST_TOKEN,
        target_chat_id="1",
        tracking=TrackingConfig(action_window_hours=2.0),
    )
    merger = MetaSignalMerger(action_window_hours=settings.tracking.action_window_hours)
    recent = _signal(
        direction="short",
        created_at=datetime.now(UTC) - timedelta(hours=1),
    )
    result = merger.merge(
        [_signal(direction="long", score=0.85)],
        recent_actions=[recent],
    )
    assert len(result.merged) == 0
    assert len(result.direction_conflicts) == 1


def test_l10_cycle_log_records_merge_conflicts() -> None:
    bot = MagicMock()
    bot.settings = BotSettings(tg_token=_TEST_TOKEN, target_chat_id="1")
    bot.telemetry = MagicMock()
    bot._shortlist_source = "live"
    bot._prepare_error_count = 0
    bot._ws_manager = None
    bot._bus = SimpleNamespace(stats=lambda: {})
    bot.client = SimpleNamespace(state_snapshot=lambda: {})
    bot.last_cycle_summary = {}
    bot.dashboard = None

    manager = TelemetryManager(bot)
    result = PipelineResult(
        symbol="BTCUSDT",
        trigger="kline_close",
        event_ts=datetime.now(UTC),
        raw_setups=4,
        funnel={"detector_runs": 4, "merge_conflicts": 2},
    )
    manager.emit_cycle_log(
        symbol="BTCUSDT",
        interval="15m",
        event_ts=result.event_ts,
        shortlist_size=20,
        tracking_events=[],
        result=result,
        candidates=[],
        rejected=[],
        delivered=[],
    )
    cycle_row = bot.telemetry.append_jsonl.call_args_list[0].args[1]
    assert cycle_row["merge_conflicts"] == 2


@pytest.mark.asyncio
async def test_l10_delivery_orchestrator_passes_action_window_to_merger() -> None:
    bot = MagicMock()
    bot.settings = BotSettings(
        tg_token=_TEST_TOKEN,
        target_chat_id="1",
        tracking=TrackingConfig(action_window_hours=6.0),
    )
    bot.public_audit = MagicMock()
    bot.public_audit.recent_action_signals.return_value = []

    orchestrator = DeliveryOrchestrator(bot)
    with patch.object(orchestrator, "_contract_issue_rows", return_value=[]):
        with patch.object(
            orchestrator,
            "_hard_confluence_gate",
            return_value=(False, 0, {"reason": "test_gate"}),
        ):
            _, _, _, merge_conflicts = await orchestrator.select_and_deliver(
                [
                    _signal(direction="long", score=0.9),
                    _signal(direction="short", score=0.7, setup_id="fvg_setup"),
                ]
            )

    bot.public_audit.recent_action_signals.assert_called_once_with(within_hours=6.0)
    assert merge_conflicts == 1


def test_l4_lite_family_gates_mixin_has_gate_methods() -> None:
    assert hasattr(AnalyzerFamilyGatesMixin, "check_family_precheck")
    assert hasattr(AnalyzerFamilyGatesMixin, "apply_alignment_penalty")
    assert hasattr(AnalyzerFamilyGatesMixin, "check_family_confirmation")
