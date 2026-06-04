"""Wave E8 agent A: lane cap, calm fits, unified pool priority, rejection stats, score floor."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from bot.domain.config import (
    AssetConfig,
    BotSettings,
    DeliveryConfig,
    FilterConfig,
    RuntimeConfig,
    UniverseConfig,
)
from bot.domain.schemas import Signal
from bot.domain.strategies import StrategyMetadata, SignalResult, StrategyDecision
from bot.engine.engine import SignalEngine
from bot.engine.lanes import select_lane_setups
from bot.market.strategy_pools import DATA_POOL_SETUPS
from bot.market.universe import strategy_fits_for_market_row
from bot.runtime.telemetry_manager import TelemetryManager
from bot.runtime_policy import effective_engine_score_floor


def _meta(strategy_id: str, *, family: str, trigger_tf: str = "15m") -> StrategyMetadata:
    return StrategyMetadata(
        strategy_id=strategy_id,
        name=strategy_id,
        family=family,
        trigger_tf=trigger_tf,
        trigger_intervals=(),
        timeframes=["15m", "1h"],
    )


class _MockRegistry:
    def __init__(self, metadata: list[StrategyMetadata]) -> None:
        self._metadata = list(metadata)

    def list_enabled(self) -> list[StrategyMetadata]:
        return list(self._metadata)


def _settings(**runtime_overrides: object) -> BotSettings:
    runtime = RuntimeConfig(**runtime_overrides)
    return BotSettings(tg_token="test", target_chat_id="1", runtime=runtime)


def test_max_setups_per_family_configurable() -> None:
    registry = _MockRegistry(
        [
            _meta("continuation_a", family="continuation"),
            _meta("continuation_b", family="continuation"),
            _meta("reversal_a", family="reversal"),
        ]
    )
    settings = _settings(
        min_setup_families_per_symbol=8,
        target_setup_families_per_symbol=8,
        max_setup_families_per_symbol=15,
        max_setups_per_family=1,
    )
    result = select_lane_setups(
        registry,
        symbol="BTCUSDT",
        interval="15m",
        settings=settings,
        strategy_fits=["continuation_a", "continuation_b", "reversal_a"],
    )
    assert [meta.strategy_id for meta in result] == ["continuation_a", "reversal_a"]


def test_calm_row_includes_reversal_microstructure_setups() -> None:
    settings = BotSettings(
        tg_token="test",
        target_chat_id="1",
        universe=UniverseConfig(min_quote_volume_usd=50_000_000),
    )
    row = {
        "symbol": "ADAUSDT",
        "quote_volume": 80_000_000.0,
        "price_change_percent": 1.2,
        "spread_bps": 4.0,
    }
    fits = strategy_fits_for_market_row(row, settings=settings, liquidity_rank=100)
    for setup_id in ("indicator_divergence", "stop_hunt_detection", "wyckoff_spring"):
        assert setup_id in fits


def test_unified_routing_prioritizes_orderflow_positioning_pools() -> None:
    registry = _MockRegistry(
        [
            _meta("structure_pullback", family="continuation"),
            _meta("cvd_divergence", family="orderflow"),
            _meta("funding_reversal", family="positioning"),
        ]
    )
    settings = _settings(
        min_setup_families_per_symbol=8,
        target_setup_families_per_symbol=8,
        max_setup_families_per_symbol=15,
    )
    pool_priority = tuple(
        sorted(
            DATA_POOL_SETUPS.get("orderflow", frozenset())
            | DATA_POOL_SETUPS.get("positioning", frozenset())
        )
    )
    without_priority = select_lane_setups(
        registry,
        symbol="ETHUSDT",
        interval="15m",
        settings=settings,
        strategy_fits=["structure_pullback", "cvd_divergence", "funding_reversal"],
    )
    with_priority = select_lane_setups(
        registry,
        symbol="ETHUSDT",
        interval="15m",
        settings=settings,
        strategy_fits=["structure_pullback", "cvd_divergence", "funding_reversal"],
        priority_setup_ids=pool_priority,
    )
    assert without_priority[0].strategy_id == "structure_pullback"
    assert with_priority[0].strategy_id in {"cvd_divergence", "funding_reversal"}


def test_lane_excluded_emitted_to_rejection_stats() -> None:
    bot = MagicMock()
    bot.settings = BotSettings(tg_token="test", target_chat_id="1")
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


def test_engine_score_floor_uses_min_watch_and_filter() -> None:
    settings = BotSettings(
        tg_token="test",
        target_chat_id="1",
        runtime=RuntimeConfig(),
        filters=FilterConfig(min_score=0.66),
        delivery=DeliveryConfig(watch_min_score=0.55),
    )
    assert effective_engine_score_floor(settings) == 0.55


def test_engine_score_floor_deep_analysis_lowers_floor() -> None:
    settings = BotSettings(
        tg_token="test",
        target_chat_id="1",
        runtime=RuntimeConfig(),
        filters=FilterConfig(min_score=0.66),
        delivery=DeliveryConfig(watch_min_score=0.55),
        assets={"XRPUSDT": AssetConfig(deep_analysis=True, primary_timeframe="1h")},
    )
    prepared = SimpleNamespace(symbol="XRPUSDT")
    assert effective_engine_score_floor(settings, prepared_or_symbol=prepared) == 0.48


def test_get_best_signal_respects_min_score_floor() -> None:
    settings = BotSettings(
        tg_token="test",
        target_chat_id="1",
        runtime=RuntimeConfig(),
        filters=FilterConfig(min_score=0.66),
        delivery=DeliveryConfig(watch_min_score=0.55),
    )
    engine = SignalEngine(MagicMock(), settings)

    def _result(score: float) -> SignalResult:
        signal = Signal(
            symbol="BTCUSDT",
            setup_id="test_setup",
            direction="long",
            score=score,
            timeframe="15m",
            entry_low=99.5,
            entry_high=100.5,
            stop=99.0,
            take_profit_1=102.0,
            take_profit_2=104.0,
        )
        return SignalResult(
            setup_id="test_setup",
            signal=signal,
            decision=StrategyDecision.signal_hit(setup_id="test_setup", signal=signal),
        )

    assert engine.get_best_signal([_result(0.54)]) is None
    best = engine.get_best_signal([_result(0.56)])
    assert best is not None
    assert best.score == 0.56
