from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from bot.application.delivery_orchestrator import DeliveryOrchestrator
from bot.signal_contract import (
    MIN_SIGNAL_RISK_REWARD,
    build_trade_plan,
    normalize_scale_weights,
    signal_contract_row,
    validate_signal_contract,
)


def make_signal(**overrides: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "symbol": "BTCUSDT",
        "setup_id": "unit_setup",
        "direction": "LONG",
        "entry_low": 100.0,
        "entry_high": 102.0,
        "stop_loss": 96.0,
        "tp1": 110.0,
        "tp2": 116.0,
        "tp3": 124.0,
        "scale_weights": (0.5, 0.3, 0.2),
        "valid_until": datetime.now(UTC) + timedelta(hours=1),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def reasons(signal: SimpleNamespace) -> set[str]:
    return {issue.reason for issue in validate_signal_contract(signal)}


def test_valid_long_signal_contract_passes() -> None:
    assert validate_signal_contract(make_signal()) == []


def test_valid_short_signal_contract_passes() -> None:
    signal = make_signal(
        direction="SHORT",
        entry_low=98.0,
        entry_high=100.0,
        stop_loss=104.0,
        tp1=90.0,
        tp2=84.0,
        tp3=76.0,
    )
    assert validate_signal_contract(signal) == []


def test_long_stop_must_be_below_entry_mid() -> None:
    assert "long_stop_not_below_entry" in reasons(make_signal(stop_loss=101.5))


def test_short_stop_must_be_above_entry_mid() -> None:
    signal = make_signal(
        direction="SHORT",
        entry_low=98.0,
        entry_high=100.0,
        stop_loss=98.5,
        tp1=90.0,
        tp2=84.0,
        tp3=76.0,
    )
    assert "short_stop_not_above_entry" in reasons(signal)


def test_long_targets_must_be_ordered_above_entry() -> None:
    assert "long_targets_not_ordered" in reasons(make_signal(tp1=103.0, tp2=102.0))


def test_short_targets_must_be_ordered_below_entry() -> None:
    signal = make_signal(
        direction="SHORT",
        entry_low=98.0,
        entry_high=100.0,
        stop_loss=104.0,
        tp1=85.0,
        tp2=90.0,
        tp3=76.0,
    )
    assert "short_targets_not_ordered" in reasons(signal)


def test_tp1_risk_reward_must_meet_minimum() -> None:
    signal = make_signal(stop_loss=99.0, tp1=102.0, tp2=116.0, tp3=124.0)
    assert "tp1_rr_below_minimum" in reasons(signal)


def test_contract_constant_documents_rr_floor() -> None:
    assert MIN_SIGNAL_RISK_REWARD == 1.5


def test_percent_scale_weights_sum_at_100_passes() -> None:
    assert validate_signal_contract(make_signal(scale_weights=(40.0, 30.0, 30.0))) == []


def test_percent_scale_weights_above_100_fail() -> None:
    assert "percent_sum_above_100" in reasons(make_signal(scale_weights=(50.0, 40.0, 20.0)))


def test_fraction_scale_weights_above_one_fail() -> None:
    assert "fraction_sum_above_one" in reasons(make_signal(scale_weights=(0.6, 0.5, 0.1)))


def test_scale_weights_need_at_least_two_allocations() -> None:
    assert "less_than_two_entry_allocations" in reasons(make_signal(scale_weights=(1.0,)))


def test_expired_signal_contract_fails() -> None:
    signal = make_signal(valid_until=datetime.now(UTC) - timedelta(seconds=1))
    assert "expired" in reasons(signal)


def test_signal_contract_row_reports_issues() -> None:
    row = signal_contract_row(make_signal(stop_loss=102.0))
    assert row["ok"] is False
    assert row["issues"]


def test_delivery_orchestrator_contract_gate_uses_shared_validator() -> None:
    issues = DeliveryOrchestrator._contract_issue_rows(make_signal(stop_loss=102.0))
    assert issues
    assert issues[0]["field"] in {"stop_loss", "risk_reward", "targets"}


def test_normalize_scale_weights_keeps_three_level_dca_sum_one() -> None:
    weights = normalize_scale_weights([50.0, 30.0, 20.0])
    assert len(weights) == 3
    assert sum(weights) == 1.0


def test_build_trade_plan_outputs_contract_valid_plan() -> None:
    plan = build_trade_plan(
        direction="LONG",
        setup_id="unit_setup",
        strategy_family="trend_follow",
        timeframe="15m",
        price_anchor=100.0,
        atr=2.0,
        stop_loss=94.0,
        tp1=105.0,
        tp2=111.0,
        tp3=120.0,
        created_at=datetime.now(UTC),
    )
    assert plan is not None
    signal = make_signal(
        direction="LONG",
        entry_low=plan.entry_low,
        entry_high=plan.entry_high,
        stop_loss=plan.stop_loss,
        tp1=plan.tp1,
        tp2=plan.tp2,
        tp3=plan.tp3,
        scale_weights=plan.scale_weights,
        valid_until=plan.valid_until,
    )
    assert validate_signal_contract(signal) == []
