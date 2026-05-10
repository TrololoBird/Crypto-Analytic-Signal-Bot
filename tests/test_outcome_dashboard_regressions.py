from __future__ import annotations

from types import SimpleNamespace

import pytest

from bot.analytics import StrategyAnalytics
from bot import dashboard as dashboard_module
from bot.config import BotSettings
from bot.core.memory.repository import MemoryRepository
from bot.outcomes import SignalFeatures, create_outcome_from_tracked
from bot.strategies import STRATEGY_CLASSES
from bot.tracked_signals import TrackedSignalState


def _tracked_with_break_even_stop() -> TrackedSignalState:
    return TrackedSignalState(
        tracking_id="BTCUSDT|ema_bounce|long|20260504T000000000000Z",
        tracking_ref="ABC12345",
        signal_key="BTCUSDT|ema_bounce|long",
        symbol="BTCUSDT",
        setup_id="ema_bounce",
        direction="long",
        timeframe="15m",
        created_at="2026-05-04T00:00:00+00:00",
        pending_expires_at="2026-05-04T00:10:00+00:00",
        active_expires_at="2026-05-04T01:00:00+00:00",
        entry_low=99.5,
        entry_high=100.5,
        entry_mid=100.0,
        initial_stop=95.0,
        stop=100.0,
        stop_price=100.0,
        take_profit_1=103.0,
        tp1_price=103.0,
        take_profit_2=110.0,
        tp2_price=110.0,
        score=0.82,
        risk_reward=2.0,
        reasons=("test",),
        status="closed",
        activated_at="2026-05-04T00:05:00+00:00",
        activation_price=100.0,
        closed_at="2026-05-04T00:20:00+00:00",
        close_reason="tp2_hit",
        close_price=110.0,
        tp1_hit_at="2026-05-04T00:10:00+00:00",
        tp2_hit_at="2026-05-04T00:20:00+00:00",
    )


def test_outcome_r_multiple_uses_initial_stop_after_break_even_move() -> None:
    outcome = create_outcome_from_tracked(
        _tracked_with_break_even_stop(),
        SignalFeatures(base_score=0.82, setup_id="ema_bounce", direction="long"),
    )

    assert outcome.pnl_pct == pytest.approx(10.0)
    assert outcome.pnl_r_multiple == pytest.approx(2.0)


def test_dashboard_strategy_cache_uses_declared_setup_ids() -> None:
    if not dashboard_module.HAS_FASTAPI:
        pytest.skip("fastapi is not installed")

    settings = BotSettings(tg_token="test-token", target_chat_id="@test_channel")
    settings.setups.fvg_setup = False
    dashboard = dashboard_module.BotDashboard(SimpleNamespace(settings=settings))

    cached_ids = [row["id"] for row in dashboard._strategies_cache or []]
    declared_ids = [cls.setup_id for cls in STRATEGY_CLASSES]

    assert cached_ids == declared_ids
    assert len(cached_ids) == 37
    assert {row["id"] for row in dashboard._strategies_cache or []} >= {
        "bos_choch",
        "fvg_setup",
        "vwap_trend",
        "rsi_divergence_bottom",
    }
    assert (
        next(
            row for row in dashboard._strategies_cache or [] if row["id"] == "fvg_setup"
        )["enabled"]
        is False
    )


@pytest.mark.asyncio
async def test_dashboard_active_signal_payload_uses_persisted_tracking_prices() -> None:
    class RepoStub:
        async def get_active_signals(self) -> list[dict[str, object]]:
            return [
                {
                    "symbol": "BTCUSDT",
                    "setup_id": "ema_bounce",
                    "direction": "long",
                    "entry_mid": 100.0,
                    "stop": 95.0,
                    "take_profit_1": 103.0,
                    "take_profit_2": 106.0,
                    "score": 0.72,
                    "risk_reward": 2.0,
                    "status": "pending",
                    "tracking_id": "tid",
                    "tracking_ref": "ABC12345",
                    "created_at": "2026-05-04T00:00:00+00:00",
                }
            ]

    settings = BotSettings(tg_token="test-token", target_chat_id="@test_channel")
    dashboard = dashboard_module.BotDashboard(
        SimpleNamespace(settings=settings, _modern_repo=RepoStub())
    )

    payload = await dashboard._get_active_signals()

    assert payload == [
        {
            "symbol": "BTCUSDT",
            "setup_id": "ema_bounce",
            "direction": "long",
            "entry_price": 100.0,
            "stop_price": 95.0,
            "tp1_price": 103.0,
            "tp2_price": 106.0,
            "score": 0.72,
            "risk_reward": 2.0,
            "status": "pending",
            "tracking_id": "tid",
            "tracking_ref": "ABC12345",
            "timestamp": "2026-05-04T00:00:00+00:00",
        }
    ]


@pytest.mark.asyncio
async def test_strategy_analytics_report_includes_dashboard_compat_fields() -> None:
    class RepoStub:
        async def get_setup_stats(
            self, *, last_days: int = 30
        ) -> list[dict[str, object]]:
            return [
                {
                    "setup_id": "ema_bounce",
                    "total": 2,
                    "win_rate": 0.5,
                    "avg_r_multiple": 0.2,
                }
            ]

        async def get_signal_outcomes(
            self, *, last_days: int = 30
        ) -> list[dict[str, object]]:
            return [
                {"setup_id": "ema_bounce", "pnl_r_multiple": 1.0},
                {"setup_id": "ema_bounce", "pnl_r_multiple": -0.6},
            ]

    report = await StrategyAnalytics(repo=RepoStub()).generate_report(days=14)  # type: ignore[arg-type]

    assert report["summary"]["total_signals"] == 2
    assert report["summary"]["win_rate"] == pytest.approx(0.5)
    assert report["summary"]["avg_rr"] == pytest.approx(0.2)
    assert report["by_setup"]["ema_bounce"]["count"] == 2
    assert report["by_setup"]["ema_bounce"]["avg_rr"] == pytest.approx(0.2)


@pytest.mark.asyncio
async def test_setup_performance_adjustment_penalizes_after_eight_poor_outcomes(
    tmp_path,
) -> None:
    repo = MemoryRepository(tmp_path / "bot.db", tmp_path)
    await repo.initialize()
    try:
        for _ in range(8):
            await repo.record_setup_outcome("spread_strategy", "stop_loss")

        assert await repo.get_setup_score_adjustment("spread_strategy") < 0.0
    finally:
        await repo.close()


@pytest.mark.asyncio
async def test_setup_performance_adjustment_uses_r_multiple_for_smart_exits(
    tmp_path,
) -> None:
    repo = MemoryRepository(tmp_path / "bot.db", tmp_path)
    await repo.initialize()
    try:
        for _ in range(8):
            await repo.record_setup_outcome(
                "liquidation_heatmap",
                "smart_exit",
                pnl_r_multiple=0.45,
                was_profitable=True,
            )

        assert await repo.get_setup_score_adjustment("liquidation_heatmap") > 0.0
    finally:
        await repo.close()


def test_dashboard_strategy_catalog_keeps_zero_outcome_strategies_visible() -> None:
    dashboard = dashboard_module.BotDashboard.__new__(dashboard_module.BotDashboard)
    dashboard._strategies_cache = [
        {
            "id": "multi_tf_trend",
            "enabled": True,
            "status": "production",
            "risk_profile": "continuation",
            "family": "continuation",
        },
        {
            "id": "price_velocity",
            "enabled": True,
            "status": "beta",
            "risk_profile": "breakout",
            "family": "breakout",
        },
    ]
    report = {
        "by_setup": {
            "multi_tf_trend": {
                "setup_id": "multi_tf_trend",
                "trades": 3,
                "count": 3,
                "win_rate": 0.66,
                "expectancy_r": 0.4,
                "avg_rr": 0.4,
                "profit_factor": 2.0,
                "max_drawdown_r": -0.2,
            }
        },
        "setup_reports": [
            {
                "setup_id": "multi_tf_trend",
                "trades": 3,
                "count": 3,
                "win_rate": 0.66,
                "expectancy_r": 0.4,
                "avg_rr": 0.4,
                "profit_factor": 2.0,
                "max_drawdown_r": -0.2,
            }
        ],
    }

    merged = dashboard._merge_strategy_catalog(report)

    assert merged["registered_strategies"] == 2
    assert merged["enabled_strategies"] == 2
    assert merged["by_setup"]["price_velocity"]["trades"] == 0
    assert merged["by_setup"]["price_velocity"]["status"] == "beta"
