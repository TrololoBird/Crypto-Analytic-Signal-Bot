from __future__ import annotations

from bot.application.symbol_analyzer import _attach_rejection_rollups


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
