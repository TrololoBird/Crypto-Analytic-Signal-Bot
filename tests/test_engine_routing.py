"""Unit tests for SignalEngine strategy routing (lanes on kline events)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from bot.domain.config import BotSettings, RuntimeConfig
from bot.domain.strategies import StrategyMetadata
from bot.engine.engine import SignalEngine


def _settings(**runtime_overrides: object) -> BotSettings:
    runtime = RuntimeConfig(**runtime_overrides)
    return BotSettings(tg_token="test", target_chat_id="1", runtime=runtime)


def _meta(strategy_id: str, *, family: str, trigger_tf: str = "15m") -> StrategyMetadata:
    return StrategyMetadata(
        strategy_id=strategy_id,
        name=strategy_id,
        family=family,
        trigger_tf=trigger_tf,
        trigger_intervals=(),
        timeframes=["15m", "1h"],
    )


class _StubStrategy:
    def __init__(self, metadata: StrategyMetadata) -> None:
        self.strategy_id = metadata.strategy_id
        self.metadata = metadata

    def can_calculate(self, _prepared: object) -> bool:
        return True


def _engine_with_strategies(
    metadata: list[StrategyMetadata],
    settings: BotSettings,
) -> SignalEngine:
    registry = MagicMock()
    strategies = [_StubStrategy(m) for m in metadata]
    registry.get_enabled.return_value = strategies
    registry.list_enabled.return_value = metadata
    return SignalEngine(registry, settings)


def test_lane_routing_limits_families_on_kline_close() -> None:
    metadata = [
        _meta(f"setup_{idx}", family=f"family_{idx % 12}", trigger_tf="15m") for idx in range(20)
    ]
    settings = _settings(
        enable_strategy_lanes=True,
        route_all_enabled_strategies=False,
        emit_strategy_routing_skips=True,
    )
    engine = _engine_with_strategies(metadata, settings)
    prepared = MagicMock()
    prepared.symbol = "BTCUSDT"
    prepared.universe = SimpleNamespace(
        strategy_fits=tuple(m.strategy_id for m in metadata[:15]),
        shortlist_score=1.0,
    )
    routed, skips = engine._route_strategies(prepared, event_interval="15m")
    families = {s.metadata.family for s in routed}
    assert len(routed) <= settings.runtime.max_setup_families_per_symbol
    per_family: dict[str, int] = {}
    for strategy in routed:
        family = strategy.metadata.family
        per_family[family] = per_family.get(family, 0) + 1
    assert all(count <= settings.runtime.max_setups_per_family for count in per_family.values())
    assert len(families) <= len(routed)
    assert len(skips) == 20 - len(routed)


def test_route_all_bypasses_lanes() -> None:
    metadata = [_meta("a", family="f1"), _meta("b", family="f2", trigger_tf="1h")]
    settings = _settings(
        enable_strategy_lanes=True,
        route_all_enabled_strategies=True,
    )
    engine = _engine_with_strategies(metadata, settings)
    prepared = MagicMock()
    prepared.symbol = "BTCUSDT"
    prepared.universe = SimpleNamespace(strategy_fits=("a",), shortlist_score=1.0)
    routed, _ = engine._route_strategies(prepared, event_interval="15m")
    assert len(routed) == 2


def test_shortlist_unified_routing_ignores_strategy_fits() -> None:
    metadata = [
        _meta("routed_setup", family="f1"),
        _meta("other_setup", family="f2"),
    ]
    settings = _settings(
        enable_strategy_lanes=True,
        route_all_enabled_strategies=False,
        shortlist_unified_routing=True,
    )
    engine = _engine_with_strategies(metadata, settings)
    prepared = MagicMock()
    prepared.symbol = "ETHUSDT"
    prepared.universe = SimpleNamespace(strategy_fits=("routed_setup",), shortlist_score=0.8)
    routed, skips = engine._route_strategies(prepared, event_interval="15m")
    routed_ids = {strategy.strategy_id for strategy in routed}
    assert "other_setup" in routed_ids
    assert "routed_setup" in routed_ids
    assert not any(
        result.decision.reason_code == "asset_fit.shortlist_not_routed" for result in skips
    )
