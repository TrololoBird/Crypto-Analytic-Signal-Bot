"""Unit tests for strategy lane selection (no network)."""

from __future__ import annotations

import pytest

from bot.domain.config import BotSettings, RuntimeConfig
from bot.domain.strategies import StrategyMetadata
from bot.engine.lanes import is_standard_kline_interval, select_lane_setups


def _meta(
    strategy_id: str,
    *,
    family: str,
    trigger_tf: str = "15m",
    trigger_intervals: tuple[str, ...] = (),
    timeframes: list[str] | None = None,
) -> StrategyMetadata:
    return StrategyMetadata(
        strategy_id=strategy_id,
        name=strategy_id,
        family=family,
        trigger_tf=trigger_tf,
        trigger_intervals=trigger_intervals,
        timeframes=timeframes or ["5m", "15m", "1h"],
    )


class _MockRegistry:
    def __init__(self, metadata: list[StrategyMetadata]) -> None:
        self._metadata = list(metadata)

    def list_enabled(self) -> list[StrategyMetadata]:
        return list(self._metadata)


def _settings(**runtime_overrides: object) -> BotSettings:
    runtime = RuntimeConfig(**runtime_overrides)
    return BotSettings(tg_token="test", target_chat_id="1", runtime=runtime)


def _twenty_family_registry() -> _MockRegistry:
    families = [
        "continuation",
        "reversal",
        "breakout",
        "volatility",
        "orderbook",
        "orderflow",
        "trend_follow",
        "microstructure",
        "sentiment",
        "multi_asset",
        "structure",
        "momentum",
        "mean_reversion",
        "liquidity",
        "positioning",
        "correlation",
        "seasonality",
        "funding",
        "divergence",
        "squeeze",
    ]
    metadata = [
        _meta(f"strategy_{idx:02d}", family=family, trigger_tf="15m")
        for idx, family in enumerate(families)
    ]
    # Second strategy in continuation family - should lose to strategy_00 in cap.
    metadata.append(_meta("strategy_dup_continuation", family="continuation", trigger_tf="15m"))
    return _MockRegistry(metadata)


def test_family_cap_limits_lane_count() -> None:
    registry = _twenty_family_registry()
    settings = _settings(
        min_setup_families_per_symbol=8,
        target_setup_families_per_symbol=12,
        max_setup_families_per_symbol=15,
    )
    result = select_lane_setups(
        registry,
        symbol="BTCUSDT",
        interval="15m",
        settings=settings,
        strategy_fits=[m.strategy_id for m in registry.list_enabled()],
    )
    assert len(result) == 12
    continuation_ids = {m.strategy_id for m in result if m.family == "continuation"}
    assert continuation_ids == {"strategy_00", "strategy_dup_continuation"}


def test_two_setups_per_family_when_limit_allows() -> None:
    registry = _MockRegistry(
        [
            _meta("continuation_a", family="continuation", trigger_tf="15m"),
            _meta("continuation_b", family="continuation", trigger_tf="15m"),
            _meta("reversal_a", family="reversal", trigger_tf="15m"),
        ]
    )
    settings = _settings(
        min_setup_families_per_symbol=8,
        target_setup_families_per_symbol=8,
        max_setup_families_per_symbol=15,
    )
    result = select_lane_setups(
        registry,
        symbol="BTCUSDT",
        interval="15m",
        settings=settings,
        strategy_fits=["continuation_a", "continuation_b", "reversal_a"],
    )
    assert [m.strategy_id for m in result] == ["continuation_a", "continuation_b", "reversal_a"]


def test_bb_squeeze_dropped_when_squeeze_setup_selected() -> None:
    registry = _MockRegistry(
        [
            _meta("squeeze_setup", family="breakout", trigger_tf="15m"),
            _meta("bb_squeeze", family="volatility", trigger_tf="15m"),
            _meta("reversal_a", family="reversal", trigger_tf="15m"),
        ]
    )
    settings = _settings(
        min_setup_families_per_symbol=8,
        target_setup_families_per_symbol=8,
        max_setup_families_per_symbol=15,
    )
    result = select_lane_setups(
        registry,
        symbol="BTCUSDT",
        interval="15m",
        settings=settings,
        strategy_fits=["squeeze_setup", "bb_squeeze", "reversal_a"],
    )
    ids = {m.strategy_id for m in result}
    assert ids == {"squeeze_setup", "reversal_a"}


def test_primary_matches_require_trigger_tf_equals_interval() -> None:
    registry = _MockRegistry(
        [
            _meta("match_15m", family="breakout", trigger_tf="15m"),
            _meta("other_1h", family="reversal", trigger_tf="1h"),
            _meta("fallback_15m", family="volatility", trigger_tf="1h", timeframes=["15m", "1h"]),
        ]
    )
    settings = _settings(allow_trigger_interval_fallback=False, allow_timeframe_fallback=False)
    result = select_lane_setups(
        registry,
        symbol="ETHUSDT",
        interval="15m",
        settings=settings,
        strategy_fits=["match_15m", "other_1h", "fallback_15m"],
    )
    assert [m.strategy_id for m in result] == ["match_15m"]


def test_strategy_fits_filters_before_interval_and_cap() -> None:
    registry = _twenty_family_registry()
    allowed = {"strategy_00", "strategy_01", "strategy_02"}
    settings = _settings(
        min_setup_families_per_symbol=8,
        target_setup_families_per_symbol=12,
        max_setup_families_per_symbol=15,
    )
    result = select_lane_setups(
        registry,
        symbol="BTCUSDT",
        interval="15m",
        settings=settings,
        strategy_fits=sorted(allowed),
    )
    assert len(result) == 3
    assert {m.strategy_id for m in result} == allowed


def test_apply_interval_filter_false_skips_interval_on_empty() -> None:
    registry = _twenty_family_registry()
    settings = _settings(
        min_setup_families_per_symbol=8,
        target_setup_families_per_symbol=10,
        max_setup_families_per_symbol=15,
    )
    result = select_lane_setups(
        registry,
        symbol="BTCUSDT",
        interval="",
        settings=settings,
        strategy_fits=[m.strategy_id for m in registry.list_enabled()],
        apply_interval_filter=False,
    )
    assert len(result) == 10
    assert len(result) == len({m.strategy_id for m in result})


def test_apply_interval_filter_true_empty_interval_returns_empty() -> None:
    registry = _twenty_family_registry()
    settings = _settings()
    result = select_lane_setups(
        registry,
        symbol="BTCUSDT",
        interval="",
        settings=settings,
        strategy_fits=[m.strategy_id for m in registry.list_enabled()],
        apply_interval_filter=True,
    )
    assert result == []


def test_non_standard_interval_with_filter_returns_empty() -> None:
    registry = _twenty_family_registry()
    settings = _settings()
    assert not is_standard_kline_interval("bogus")
    result = select_lane_setups(
        registry,
        symbol="BTCUSDT",
        interval="bogus",
        settings=settings,
        strategy_fits=[m.strategy_id for m in registry.list_enabled()],
    )
    assert result == []


def test_non_standard_interval_without_filter_still_caps_families() -> None:
    registry = _twenty_family_registry()
    settings = _settings(target_setup_families_per_symbol=8)
    result = select_lane_setups(
        registry,
        symbol="BTCUSDT",
        interval="bogus",
        settings=settings,
        strategy_fits=[m.strategy_id for m in registry.list_enabled()],
        apply_interval_filter=False,
    )
    assert len(result) == 8
    assert len(result) == len({m.strategy_id for m in result})


@pytest.mark.parametrize(
    ("interval", "expected"),
    [
        ("15m", True),
        ("1h", True),
        ("", False),
        ("not-an-interval", False),
    ],
)
def test_is_standard_kline_interval(interval: str, *, expected: bool) -> None:
    assert is_standard_kline_interval(interval) is expected
