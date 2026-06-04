from __future__ import annotations

from bot.dashboard.user_summary import build_funnel_hint, reject_reason_ru, result_label_ru


def test_build_funnel_hint_with_deliveries() -> None:
    hint = build_funnel_hint(
        overview={"last_cycle_delivered": 3, "last_cycle_candidates": 40, "btc_bias": "downtrend"},
        funnel={"cycle_totals": {"candidate_count": 40, "delivered_count": 3}},
    )
    assert hint["delivered"] == 3
    assert "отправлено 3" in hint["text"]
    assert "BTC" in hint["text"]


def test_build_funnel_hint_zero_delivered() -> None:
    hint = build_funnel_hint(
        overview={
            "last_cycle_candidates": 12,
            "top_rejection": {"key": "score_too_low", "count": 9},
        },
        funnel={},
    )
    assert hint["delivered"] == 0
    assert "низкая оценка" in hint["text"]


def test_result_label_ru() -> None:
    assert result_label_ru("tp1_hit") == "TP1 ✓"
    assert reject_reason_ru("score_too_low") == "низкая оценка"
