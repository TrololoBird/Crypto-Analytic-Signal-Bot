from __future__ import annotations

from bot.application.symbol_analyzer import (
    _apply_setup_score_adjustment,
    _attach_rejection_rollups,
)
from bot.domain.schemas import Signal


def test_rejection_rollups_preserve_stage_and_setup_reasons() -> None:
    funnel: dict[str, object] = {}
    rejected = [
        {"stage": "filters", "setup_id": "ema_bounce", "reason": "score_too_low"},
        {"stage": "filters", "setup_id": "ema_bounce", "reason": "score_too_low"},
        {"stage": "confirmation", "setup_id": "fvg_setup", "reason": "5m_opposes_long"},
    ]

    _attach_rejection_rollups(funnel, rejected)

    assert funnel["rejects_by_stage"] == {"filters": 2, "confirmation": 1}
    assert funnel["rejects_by_setup"] == {"ema_bounce": 2, "fvg_setup": 1}
    assert funnel["reject_reasons_by_stage"] == {
        "filters:score_too_low": 2,
        "confirmation:5m_opposes_long": 1,
    }
    assert funnel["reject_reasons_by_setup"] == {
        "ema_bounce:score_too_low": 2,
        "fvg_setup:5m_opposes_long": 1,
    }


def test_setup_score_adjustment_penalizes_without_hard_suppressing_signal() -> None:
    signal = Signal(
        symbol="BTCUSDT",
        setup_id="spread_strategy",
        direction="long",
        score=0.58,
        timeframe="15m",
        entry_low=100.0,
        entry_high=100.0,
        stop=98.0,
        take_profit_1=103.0,
        take_profit_2=104.0,
        reasons=("tight_spread_long",),
    )

    adjusted, details = _apply_setup_score_adjustment(signal, -0.05)

    assert adjusted.score == 0.53
    assert adjusted.setup_id == signal.setup_id
    assert "setup_performance_penalty" in adjusted.reasons
    assert details == {
        "applied": True,
        "adjustment": -0.05,
        "score_before": 0.58,
        "score_after": 0.53,
        "reason": "setup_performance_penalty",
    }
