"""Tests for stop-loss root cause classification."""

from __future__ import annotations

from bot.persistence.sl_diagnostics import classify_stop_loss_root_cause


def test_bear_long_immediate_stop() -> None:
    diag = classify_stop_loss_root_cause(
        direction="long",
        mfe=0.0,
        mae=1.2,
        time_to_entry_min=2,
        time_to_exit_min=8,
        features={"market_regime": "bear", "btc_bias": "downtrend"},
    )
    assert diag["code"] == "bear_long_immediate_stop"
    assert any("bear" in reason for reason in diag["reasons"])


def test_stop_hunt_post_recovery() -> None:
    diag = classify_stop_loss_root_cause(
        direction="long",
        mfe=0.0,
        mae=1.0,
        time_to_entry_min=5,
        time_to_exit_min=20,
        features={"post_sl_favorable_pct": 2.5, "post_sl_tp1_room_pct": 3.0},
    )
    assert diag["code"] == "stop_hunt_post_recovery"
