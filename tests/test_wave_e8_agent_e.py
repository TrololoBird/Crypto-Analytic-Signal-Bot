"""Wave E8 agent E — freshness_5m, MTF strict HTF, TF fallback badge, filter stages."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

from bot.delivery.filter_stages import DEFAULT_FILTER_STAGES, enabled_filter_stages
from bot.delivery.filters import _primary_freshness_window
from bot.delivery.formatting import (
    SignalMessageFacts,
    format_channel_trade_card,
    _primary_timeframe_fallback_badge,
)
from bot.domain.config import BotSettings, FilterConfig, RuntimeConfig
from bot.domain.mtf import evaluate_mtf_gate, normalize_mtf_reject_reason


def _settings(**filter_overrides: object) -> BotSettings:
    return BotSettings(
        tg_token="test",
        target_chat_id="1",
        filters=FilterConfig(**filter_overrides),  # type: ignore[arg-type]
    )


def test_primary_freshness_window_uses_freshness_5m_minutes() -> None:
    settings = _settings(freshness_5m_minutes=7, freshness_15m_minutes=16)
    prepared = SimpleNamespace(primary_timeframe="5m")
    timeframe, window = _primary_freshness_window(prepared, settings)
    assert timeframe == "5m"
    assert window == timedelta(minutes=7)


def test_primary_freshness_window_15m_unchanged() -> None:
    settings = _settings(freshness_5m_minutes=7, freshness_15m_minutes=16)
    prepared = SimpleNamespace(primary_timeframe="15m")
    timeframe, window = _primary_freshness_window(prepared, settings)
    assert timeframe == "15m"
    assert window == timedelta(minutes=16)


def test_mtf_gate_strict_rejects_missing_htf_frames() -> None:
    prepared = SimpleNamespace(work_1h=None, work_4h=None)
    ok, reason, details = evaluate_mtf_gate(
        prepared,
        "long",
        confirmation_profile="trend_follow",
        strict_data_quality=True,
    )
    assert ok is False
    assert reason == "htf_frames_missing"
    assert details.get("frames") == []


def test_mtf_gate_relaxed_passes_missing_htf_frames() -> None:
    prepared = SimpleNamespace(work_1h=None, work_4h=None)
    ok, reason, _ = evaluate_mtf_gate(
        prepared,
        "long",
        confirmation_profile="trend_follow",
        strict_data_quality=False,
    )
    assert ok is True
    assert reason == "mtf_frames_missing"


def test_normalize_mtf_reject_reason_htf_frames_missing() -> None:
    assert normalize_mtf_reject_reason("htf_frames_missing") == "htf_frames_missing"


def test_primary_timeframe_fallback_badge_on_trade_card() -> None:
    facts = SignalMessageFacts(
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
        take_profit_3=None,
        risk_reward=2.0,
        stop_distance_pct=2.0,
        valid_until=None,
        passed_filters=("primary_timeframe_fallback:5m",),
    )
    badge = _primary_timeframe_fallback_badge(facts.passed_filters, actual_timeframe=facts.timeframe)
    assert badge == "5m→15m"
    card = format_channel_trade_card(facts, include_chart=False)
    assert "5m→15m" in card


def test_enabled_stages_respects_config_and_defaults() -> None:
    settings = _settings(
        enabled_stages=("freshness", "spread", "min_score"),
    )
    enabled = enabled_filter_stages(settings)
    assert enabled == frozenset({"freshness", "spread", "min_score"})

    default_settings = _settings()
    assert enabled_filter_stages(default_settings) == frozenset(DEFAULT_FILTER_STAGES)


def test_config_example_documents_freshness_5m_and_filter_stages() -> None:
    example = Path("config.toml.example").read_text(encoding="utf-8")
    assert "freshness_5m_minutes" in example
    assert "filter_stages.py" in example
    assert 'enabled_stages = ["freshness"' in example
    assert "5m" in example and "REST" in example
    assert 'kline_intervals = ["15m"]' in example


def test_config_example_strict_data_quality_flag() -> None:
    example = Path("config.toml.example").read_text(encoding="utf-8")
    assert "strict_data_quality = true" in example


def test_runtime_strict_flag_controls_mtf_missing_frames() -> None:
    settings = BotSettings(
        tg_token="test",
        target_chat_id="1",
        runtime=RuntimeConfig(strict_data_quality=False),
    )
    prepared = SimpleNamespace(work_1h=None, work_4h=None, settings=settings)
    ok, reason, _ = evaluate_mtf_gate(
        prepared,
        "short",
        confirmation_profile="breakout_acceptance",
        strict_data_quality=bool(settings.runtime.strict_data_quality),
    )
    assert ok is True
    assert reason == "mtf_frames_missing"
