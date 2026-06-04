"""Wave F10 agent Q — diagnostics routing, health adapter, quality monitor audit."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bot.dashboard.live import DashboardLiveData
from bot.dashboard.live_audit import audit_snapshot, build_dashboard_audit_snapshot
from bot.diagnostics.config_audit import (
    audit_runtime_config,
    audit_shortlist_zero_fit,
    run_full_audit,
)
from bot.diagnostics.runtime.health import bot_runtime_health_check
from bot.diagnostics.signals import SignalDiagnostics, set_global_diagnostics
from bot.domain.config import BotSettings, RuntimeConfig
from bot.engine.engine import SignalEngine
from bot.runtime_policy import effective_shortlist_unified_routing


def _empty_snapshot(**overrides: object) -> dict:
    base = build_dashboard_audit_snapshot(
        overview={"running": True},
        funnel={"cycle_totals": {"cycles": 0}},
        shortlist={"total": 50, "dynamic": 50, "zero_fit": 40, "source": "rest_full"},
        decisions={"total_rows": 10, "status_counts": {"signal": 2}},
        rejections={"total_rows": 0, "reasons": []},
        delivery={"selected_count": 1, "delivery_count": 1},
        runtime={
            "shortlist_unified_routing": True,
            "effective_shortlist_unified_routing": True,
        },
        telegram={"available": True, "preview": {"ok": True, "chars": 500}},
    )
    base.update(overrides)
    return base


def test_effective_shortlist_unified_routing_requires_nonempty_shortlist() -> None:
    runtime = RuntimeConfig(shortlist_unified_routing=True)
    assert effective_shortlist_unified_routing(runtime, shortlist_total=0) is False
    assert effective_shortlist_unified_routing(runtime, shortlist_total=12) is True
    assert effective_shortlist_unified_routing(
        RuntimeConfig(shortlist_unified_routing=False),
        shortlist_total=12,
    ) is False


def test_audit_shortlist_zero_fit_warns_under_unified_routing() -> None:
    warnings = audit_shortlist_zero_fit(
        zero_fit=30,
        shortlist_total=50,
        unified_routing=True,
    )
    assert len(warnings) == 1
    assert "zero_strategy_fit=30/50" in warnings[0]
    assert audit_shortlist_zero_fit(zero_fit=30, shortlist_total=50, unified_routing=False) == []


def test_audit_runtime_config_flags_unified_and_emit_skips() -> None:
    settings = SimpleNamespace(
        runtime=SimpleNamespace(
            shortlist_unified_routing=True,
            emit_strategy_routing_skips=False,
            enable_strategy_lanes=True,
        )
    )
    warnings = audit_runtime_config(settings)
    joined = " ".join(warnings)
    assert "shortlist_unified_routing=true" in joined
    assert "emit_strategy_routing_skips=false" in joined


def test_run_full_audit_includes_runtime_warnings() -> None:
    settings = SimpleNamespace(
        filters=SimpleNamespace(
            min_atr_pct=0.40,
            max_atr_pct=10.0,
            min_score=0.66,
            min_risk_reward=1.9,
            min_adx_1h=20.0,
            cooldown_minutes=60,
        ),
        runtime=SimpleNamespace(
            enable_strategy_lanes=True,
            route_all_enabled_strategies=False,
            min_setup_families_per_symbol=8,
            target_setup_families_per_symbol=12,
            max_setup_families_per_symbol=15,
            shortlist_unified_routing=True,
            emit_strategy_routing_skips=False,
        ),
        delivery=SimpleNamespace(
            action_min_score=0.72,
            watch_min_score=0.55,
            action_cap_per_cycle=6,
            watch_cap_per_cycle=12,
        ),
        universe=SimpleNamespace(
            pinned_symbols=("BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "XAUUSDT", "XAGUSDT", "PAXGUSDT"),
            min_quote_volume_usd=50_000_000,
            min_price_change_pct=0.5,
            shortlist_limit=45,
        ),
        setups=SimpleNamespace(
            enabled_setup_ids=lambda: tuple(f"setup_{idx:02d}" for idx in range(12))
        ),
    )
    result = run_full_audit(settings)
    assert len(result["runtime_warnings"]) >= 2


def test_live_runtime_snapshot_effective_unified_and_quality_monitor() -> None:
    bot = MagicMock()
    bot._shortlist = [object(), object()]
    bot.settings = BotSettings(
        tg_token="123456789:ABCdefGHIjklMNOpqrsTUVwxyz",
        target_chat_id="1",
        runtime=RuntimeConfig(shortlist_unified_routing=True),
    )
    bot._ws_manager = None
    bot._signal_diagnostics = SignalDiagnostics()
    bot.quality_monitor = MagicMock()
    bot.quality_monitor.telemetry_snapshot.return_value = {
        "quality_monitor": {
            "recommendations": {"pause": 1},
            "unhealthy_setups": ["ema_bounce"],
        }
    }

    live = DashboardLiveData(lambda: bot)
    live._iter_recent = lambda *_args, **_kwargs: []  # type: ignore[method-assign]

    payload = live._runtime_uncached()

    assert payload["effective_shortlist_unified_routing"] is True
    assert payload["shortlist_total"] == 2
    assert payload["quality_monitor"]["recommendations"]["pause"] == 1


def test_audit_zero_fit_suppressed_when_effective_unified_routing() -> None:
    snap = _empty_snapshot()
    report = audit_snapshot(snap)
    codes = {row["code"] for row in report["findings"]}
    assert "strategy_routing_empty" not in codes


def test_audit_quality_monitor_pause_finding() -> None:
    snap = _empty_snapshot(
        runtime={
            "shortlist_unified_routing": True,
            "effective_shortlist_unified_routing": True,
            "quality_monitor": {
                "recommendations": {"pause": 2, "keep": 5},
                "unhealthy_setups": ["rsi_div", "ema_bounce"],
            },
        }
    )
    report = audit_snapshot(snap)
    finding = next(row for row in report["findings"] if row["code"] == "quality_monitor_pause")
    assert finding["severity"] == "warning"
    assert finding["evidence"]["pause_count"] == 2


def test_record_routing_skip_wired_from_engine_lane_exclusion() -> None:
    diagnostics = SignalDiagnostics()
    set_global_diagnostics(diagnostics)
    metadata = [
        SimpleNamespace(
            strategy_id=f"setup_{idx}",
            name=f"setup_{idx}",
            family=f"family_{idx % 12}",
            trigger_tf="15m",
            trigger_intervals=(),
            timeframes=["15m", "1h"],
        )
        for idx in range(20)
    ]

    class _StubStrategy:
        def __init__(self, meta: SimpleNamespace) -> None:
            self.strategy_id = meta.strategy_id
            self.metadata = meta

    registry = MagicMock()
    strategies = [_StubStrategy(m) for m in metadata]
    registry.get_enabled.return_value = strategies
    settings = BotSettings(
        tg_token="123456789:ABCdefGHIjklMNOpqrsTUVwxyz",
        target_chat_id="1",
        runtime=RuntimeConfig(
            enable_strategy_lanes=True,
            emit_strategy_routing_skips=True,
        ),
    )
    engine = SignalEngine(registry, settings)
    prepared = MagicMock()
    prepared.symbol = "BTCUSDT"
    prepared.universe = SimpleNamespace(
        strategy_fits=tuple(m.strategy_id for m in metadata[:15]),
        shortlist_score=1.0,
    )

    _routed, skips = engine._route_strategies(prepared, event_interval="15m")

    summary = diagnostics.get_summary()
    assert len(skips) > 0
    assert summary["routing_skips_total"] == len(skips)
    assert summary["routing_skips_by_reason"]["runtime.strategy_lane_excluded"] == len(skips)


@pytest.mark.asyncio
async def test_bot_runtime_health_check_delegates_to_health_manager() -> None:
    bot = MagicMock()
    expected = {"status": "healthy", "ws_connected": True}
    with patch(
        "bot.runtime.health_manager.HealthManager.health_check",
        new_callable=AsyncMock,
        return_value=expected,
    ) as health_check:
        payload = await bot_runtime_health_check(bot)
    health_check.assert_awaited_once()
    assert payload == expected
