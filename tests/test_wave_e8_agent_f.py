"""Wave E8 agent F — funnel widget, unified blocker, telemetry mismatch, profile recs."""

from __future__ import annotations

from bot.dashboard.live import (
    DashboardLiveData,
    _build_funnel_widget,
    _compute_cycle_totals,
    _compute_session_delta,
    _unified_top_blocker,
)
from bot.domain.labels import (
    confluence_profile_recommendation_ru,
    normalize_reject_reason,
    reject_reason_ru,
)


def test_funnel_widget_stages_with_session_delta() -> None:
    cycles = [
        {"candidate_count": 4, "selected_count": 2, "delivered_count": 1},
        {"candidate_count": 2, "selected_count": 1, "delivered_count": 0},
    ]
    totals = _compute_cycle_totals(cycles)
    delta = _compute_session_delta(cycles)
    totals["session_delta"] = delta
    widget = _build_funnel_widget(totals, delta)

    assert totals["candidates"] == 6
    assert totals["selected"] == 3
    assert totals["delivered"] == 1
    assert delta == {"candidates": 4, "selected": 2, "delivered": 1}

    by_key = {stage["key"]: stage for stage in widget["stages"]}
    assert by_key["candidates"]["count"] == 6
    assert by_key["candidates"]["session_delta"] == 4
    assert by_key["selected"]["label_ru"] == "отобрано"
    assert by_key["delivered"]["count"] == 1


def test_unified_top_blocker_merges_normalized_sources() -> None:
    from collections import Counter

    blocker = _unified_top_blocker(
        rejected_counter=Counter(
            {
                normalize_reject_reason("hard_confluence_gate_failed"): 3,
                normalize_reject_reason("score_too_low"): 1,
            }
        ),
        decision_counter=Counter(
            {
                normalize_reject_reason("pattern.no_hit"): 5,
                normalize_reject_reason("score_too_low"): 2,
            }
        ),
    )
    assert blocker is not None
    assert blocker["key"] == "pattern.no_hit"
    assert blocker["count"] == 5
    assert blocker["decision_count"] == 5
    assert blocker["rejected_count"] == 0
    assert blocker["label_ru"] == reject_reason_ru("pattern.no_hit")
    assert "strategy_decisions" in blocker["sources"]

    merged_score = _unified_top_blocker(
        rejected_counter=Counter({normalize_reject_reason("score_too_low"): 4}),
        decision_counter=Counter({normalize_reject_reason("score_too_low"): 2}),
    )
    assert merged_score is not None
    assert merged_score["key"] == "score_too_low"
    assert merged_score["count"] == 6
    assert merged_score["rejected_count"] == 4
    assert merged_score["decision_count"] == 2
    assert set(merged_score["sources"]) == {"rejected", "strategy_decisions"}


def test_funnel_exposes_top_blocker_and_widget() -> None:
    live = DashboardLiveData(lambda: None)

    def _fake_iter(stem: str, *, max_rows: int, limit_files: int):  # noqa: ARG001
        if stem == "cycles":
            yield {"candidate_count": 3, "selected_count": 1, "delivered_count": 0}
        elif stem == "rejected":
            yield {"reason": "hard_confluence_gate_failed", "stage": "confluence"}
            yield {"reason": "score_too_low", "stage": "filters"}
        elif stem == "strategy_decisions":
            yield {"status": "reject", "reason_code": "pattern.no_hit"}
            yield {"status": "reject", "reason_code": "pattern.no_hit"}
            yield {"status": "signal", "reason_code": "pattern.hit"}
        elif stem == "selected":
            yield {"symbol": "BTCUSDT", "setup_id": "ema_bounce"}
        elif stem == "delivery":
            return
        elif stem == "selected":
            yield {"symbol": "BTCUSDT", "setup_id": "ema_bounce"}

    live._iter_recent = _fake_iter  # type: ignore[method-assign]
    payload = live._funnel_uncached(max_rows=100)

    assert payload["funnel_widget"]["stages"][0]["key"] == "candidates"
    assert payload["cycle_totals"]["session_delta"]["candidates"] == 3
    assert payload["top_blocker"]["key"] == "pattern.no_hit"
    assert payload["top_blocker"]["count"] == 2
    assert payload["combined_reject_hint"]["key"] == "pattern.no_hit"


def test_telemetry_mismatch_summary_when_rows_exist() -> None:
    live = DashboardLiveData(lambda: None)

    def _fake_iter(stem: str, *, max_rows: int, limit_files: int):  # noqa: ARG001
        if stem != "telemetry_mismatch":
            return
        yield {"mismatch_type": "candidate_count_drift"}
        yield {"mismatch_type": "candidate_count_drift"}
        yield {"mismatch_type": "delivery_status_missing"}

    live._iter_recent = _fake_iter  # type: ignore[method-assign]
    live._jsonl_refs = lambda *_a, **_k: [object()]  # type: ignore[method-assign, return-value]
    payload = live._telemetry_mismatch_uncached(max_rows=100)

    assert payload["available"] is True
    assert payload["total_rows"] == 3
    counts = {row["key"]: row["count"] for row in payload["counts"]}
    assert counts["candidate_count_drift"] == 2
    assert counts["delivery_status_missing"] == 1


def test_telemetry_mismatch_unavailable_without_file() -> None:
    live = DashboardLiveData(lambda: None)
    live._iter_recent = lambda *_a, **_k: iter(())  # type: ignore[method-assign]
    live._jsonl_refs = lambda *_a, **_k: []  # type: ignore[method-assign]
    payload = live._telemetry_mismatch_uncached()
    assert payload["available"] is False
    assert payload["total_rows"] == 0


def test_confluence_legs_by_profile_recommendation() -> None:
    live = DashboardLiveData(lambda: None)

    def _fake_iter(_stem: str, *, max_rows: int, limit_files: int):  # noqa: ARG001
        yield {
            "stage": "confluence",
            "confirmations": {
                "trend": False,
                "momentum": True,
                "volume": False,
                "htf": True,
                "microstructure": True,
            },
            "details": {"confirmation_profile": "trend_follow"},
        }
        yield {
            "stage": "confluence",
            "confirmations": {
                "trend": False,
                "momentum": False,
                "volume": True,
                "htf": True,
                "microstructure": True,
            },
            "details": {"confirmation_profile": "trend_follow"},
        }

    live._iter_recent = _fake_iter  # type: ignore[method-assign]
    payload = live._confluence_legs_by_profile_uncached(max_rows=100)
    profile = payload["profiles"][0]
    assert profile["top_failing_leg"] == "trend"
    assert profile["recommendation"]
    assert "тренд" in profile["recommendation"]
    assert "2 отказов" in profile["recommendation"]


def test_confluence_profile_recommendation_ru_empty_without_leg() -> None:
    assert confluence_profile_recommendation_ru("trend_follow", top_leg=None, leg_count=0) == ""


def test_overview_session_delivered_from_delivery_jsonl() -> None:
    live = DashboardLiveData(lambda: None)

    def _fake_iter(stem: str, *, max_rows: int, limit_files: int):  # noqa: ARG001
        if stem == "cycles":
            yield {"candidate_count": 5, "selected_count": 2, "delivered_count": 99}
        elif stem == "delivery":
            yield {"symbol": "BTCUSDT", "setup_id": "ema_bounce", "delivery_status": "sent"}
        elif stem in {"rejected", "strategy_decisions", "health_runtime", "selected"}:
            return

    live._iter_recent = _fake_iter  # type: ignore[method-assign]
    bot = type(
        "Bot",
        (),
        {
            "_shutdown": type("S", (), {"is_set": lambda self: False})(),
            "_shortlist": [],
            "_shortlist_source": "test",
            "settings": type("Settings", (), {"notifiers": type("N", (), {"provider": "none"})()})(),
        },
    )()
    live._bot_getter = lambda: bot
    overview = live._overview_uncached()
    assert overview["session_delivered"] == 1
    assert overview["funnel_widget"]["stages"][-1]["key"] == "delivered"
