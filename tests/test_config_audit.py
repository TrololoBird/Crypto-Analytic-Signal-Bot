"""Unit tests for config audit (no network)."""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import TYPE_CHECKING

from bot.diagnostics.config_audit import run_full_audit, run_startup_audit
from bot.domain.config import BotSettings, DeliveryConfig, FilterConfig

if TYPE_CHECKING:
    import pytest


def _settings(**overrides: object) -> BotSettings:
    base: dict[str, object] = {"tg_token": "test", "target_chat_id": "1"}
    base.update(overrides)
    return BotSettings(**base)


def _minimal_settings(**overrides: object) -> SimpleNamespace:
    """Lightweight settings object for audit rules without pydantic cross-checks."""
    defaults = {
        "filters": SimpleNamespace(
            min_atr_pct=0.40,
            max_atr_pct=10.0,
            min_score=0.66,
            min_risk_reward=1.9,
            min_adx_1h=20.0,
            cooldown_minutes=60,
        ),
        "runtime": SimpleNamespace(
            enable_strategy_lanes=True,
            route_all_enabled_strategies=False,
            min_setup_families_per_symbol=8,
            target_setup_families_per_symbol=12,
            max_setup_families_per_symbol=15,
            shortlist_unified_routing=False,
            emit_strategy_routing_skips=True,
        ),
        "delivery": SimpleNamespace(
            action_min_score=0.72,
            watch_min_score=0.55,
            action_cap_per_cycle=6,
            watch_cap_per_cycle=12,
        ),
        "universe": SimpleNamespace(
            pinned_symbols=(
                "BTCUSDT",
                "ETHUSDT",
                "SOLUSDT",
                "XRPUSDT",
                "XAUUSDT",
                "XAGUSDT",
                "PAXGUSDT",
            ),
            min_quote_volume_usd=50_000_000,
            min_price_change_pct=0.5,
            shortlist_limit=45,
        ),
        "setups": SimpleNamespace(
            enabled_setup_ids=lambda: tuple(f"setup_{idx:02d}" for idx in range(12))
        ),
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def test_run_full_audit_clean_defaults() -> None:
    result = run_full_audit(_minimal_settings())
    assert result["total_issues"] == 0
    for key in (
        "filter_warnings",
        "lane_warnings",
        "runtime_warnings",
        "delivery_warnings",
        "universe_warnings",
        "strategy_warnings",
    ):
        assert result[key] == []


def test_run_full_audit_flags_aggressive_filters() -> None:
    settings = _minimal_settings(
        filters=SimpleNamespace(
            min_atr_pct=2.5,
            max_atr_pct=10.0,
            min_score=0.85,
            min_risk_reward=5.0,
            min_adx_1h=35.0,
            cooldown_minutes=300,
        )
    )
    result = run_full_audit(settings)
    assert len(result["filter_warnings"]) >= 4
    joined = " ".join(result["filter_warnings"])
    assert "min_atr_pct=2.50" in joined
    assert "min_score=0.85" in joined
    assert "min_risk_reward=5.00" in joined
    assert "cooldown_minutes=300" in joined


def test_run_full_audit_flags_lanes_delivery_universe_strategies() -> None:
    settings = _minimal_settings(
        runtime=SimpleNamespace(
            enable_strategy_lanes=False,
            route_all_enabled_strategies=True,
            min_setup_families_per_symbol=13,
            target_setup_families_per_symbol=12,
            max_setup_families_per_symbol=15,
        ),
        delivery=SimpleNamespace(
            action_min_score=0.85,
            watch_min_score=0.90,
            action_cap_per_cycle=1,
            watch_cap_per_cycle=0,
        ),
        universe=SimpleNamespace(
            pinned_symbols=("BTCUSDT", "ETHUSDT"),
            min_quote_volume_usd=80_000_000,
            min_price_change_pct=4.0,
            shortlist_limit=20,
        ),
        setups=SimpleNamespace(enabled_setup_ids=lambda: ("structure_pullback",)),
    )
    result = run_full_audit(settings)
    assert result["total_issues"] >= 6
    assert any("enable_strategy_lanes=false" in msg for msg in result["lane_warnings"])
    assert any("action_min_score=0.85" in msg for msg in result["delivery_warnings"])
    assert any("pinned_symbols missing" in msg for msg in result["universe_warnings"])
    assert any("only 1 setup" in msg for msg in result["strategy_warnings"])


def test_run_startup_audit_logs_warnings(caplog: pytest.LogCaptureFixture) -> None:
    settings = _minimal_settings(
        filters=SimpleNamespace(
            min_atr_pct=2.5,
            max_atr_pct=10.0,
            min_score=0.66,
            min_risk_reward=1.9,
            min_adx_1h=20.0,
            cooldown_minutes=60,
        )
    )
    with caplog.at_level(logging.INFO, logger="bot.config_audit"):
        run_startup_audit(settings)
    assert any("CONFIG AUDIT" in record.message for record in caplog.records)
    assert any("min_atr_pct=2.50" in record.message for record in caplog.records)


def test_bot_settings_delivery_coherence_warning() -> None:
    settings = _settings(
        filters=FilterConfig(min_score=0.75),
        delivery=DeliveryConfig(action_min_score=0.72, watch_min_score=0.55),
    )
    result = run_full_audit(settings)
    assert any("filters.min_score" in msg for msg in result["delivery_warnings"])
