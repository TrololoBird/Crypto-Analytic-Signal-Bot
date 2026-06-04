"""Tests for outcome-driven shortlist deranking."""

from __future__ import annotations

from bot.market.outcome_derank import decay_weight, penalties_from_sl_counts


def test_penalties_from_sl_counts_cluster() -> None:
    penalties = penalties_from_sl_counts(
        {"BTCUSDT": 1, "ETHUSDT": 2, "SOLUSDT": 3},
        cluster_threshold=2,
        penalty_per_sl=0.08,
        max_penalty=0.28,
    )
    assert "BTCUSDT" not in penalties
    assert penalties["ETHUSDT"] == 0.08
    assert penalties["SOLUSDT"] == 0.16


def test_penalties_from_sl_counts_old_events_decay_below_threshold() -> None:
    penalties = penalties_from_sl_counts(
        {"ETHUSDT": 2},
        sl_event_ages_days={"ETHUSDT": [6.5, 6.8]},
        cluster_threshold=2,
        half_life_days=3.0,
    )
    assert "ETHUSDT" not in penalties
    assert decay_weight(6.5) < 0.25
