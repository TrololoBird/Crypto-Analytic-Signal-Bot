"""Wave E7 — confluence leg breakdown by confirmation_profile."""

from __future__ import annotations

from bot.dashboard.live import DashboardLiveData
from bot.domain.labels import (
    CONFIRMATION_PROFILE_LABEL_RU,
    confirmation_profile_label_ru,
    labels_payload,
)


def test_confirmation_profile_labels_payload() -> None:
    payload = labels_payload()
    assert "confirmation_profiles" in payload
    assert payload["confirmation_profiles"]["trend_follow"] == CONFIRMATION_PROFILE_LABEL_RU[
        "trend_follow"
    ]
    assert confirmation_profile_label_ru("breakout_acceptance") == CONFIRMATION_PROFILE_LABEL_RU[
        "breakout_acceptance"
    ]


def test_confluence_legs_by_profile_groups_failures() -> None:
    live = DashboardLiveData(lambda: None)

    def _fake_iter(_stem: str, *, max_rows: int, limit_files: int):  # noqa: ARG001
        yield {
            "stage": "confluence",
            "reason": "hard_confluence_gate",
            "confirmations": {
                "trend": False,
                "momentum": True,
                "volume": False,
                "htf": False,
                "microstructure": True,
            },
            "details": {"confirmation_profile": "trend_follow"},
        }
        yield {
            "stage": "confluence",
            "reason": "hard_confluence_gate",
            "confirmations": {
                "trend": True,
                "momentum": False,
                "volume": True,
                "htf": False,
                "microstructure": False,
            },
            "confirmation_profile": "breakout_acceptance",
        }
        yield {
            "stage": "confluence",
            "reason": "hard_confluence_gate",
            "confirmations": {
                "trend": False,
                "momentum": False,
                "volume": False,
                "htf": True,
                "microstructure": True,
            },
            "details": {"confirmation_profile": "trend_follow"},
        }
        yield {
            "stage": "filters",
            "reason": "score_too_low",
        }

    live._iter_recent = _fake_iter  # type: ignore[method-assign]
    payload = live._confluence_legs_by_profile_uncached(max_rows=100)

    assert payload["gate_rejects"] == 3
    by_key = {row["key"]: row for row in payload["profiles"]}
    assert set(by_key) == {"trend_follow", "breakout_acceptance"}

    trend = by_key["trend_follow"]
    assert trend["gate_rejects"] == 2
    assert trend["label_ru"] == confirmation_profile_label_ru("trend_follow")
    trend_counts = {row["key"]: row["count"] for row in trend["leg_failures"]}
    assert trend_counts["trend"] == 2
    assert trend_counts["volume"] == 2
    assert trend_counts["htf"] == 1
    assert trend_counts["momentum"] == 1
    assert trend["total_leg_failures"] == 6

    breakout = by_key["breakout_acceptance"]
    assert breakout["gate_rejects"] == 1
    breakout_counts = {row["key"]: row["count"] for row in breakout["leg_failures"]}
    assert breakout_counts["momentum"] == 1
    assert breakout_counts["htf"] == 1
    assert breakout_counts["microstructure"] == 1
    assert breakout["total_leg_failures"] == 3

    known = {row["key"] for row in payload["known_profiles"]}
    assert known == set(CONFIRMATION_PROFILE_LABEL_RU)


def test_confluence_legs_by_profile_normalizes_unknown_profile() -> None:
    live = DashboardLiveData(lambda: None)

    def _fake_iter(_stem: str, *, max_rows: int, limit_files: int):  # noqa: ARG001
        yield {
            "stage": "confluence",
            "confirmations": {
                "trend": False,
                "momentum": True,
                "volume": True,
                "htf": True,
                "microstructure": True,
            },
            "details": {"confirmation_profile": "unknown_profile"},
        }

    live._iter_recent = _fake_iter  # type: ignore[method-assign]
    payload = live._confluence_legs_by_profile_uncached(max_rows=100)

    assert len(payload["profiles"]) == 1
    assert payload["profiles"][0]["key"] == "trend_follow"
    assert payload["profiles"][0]["leg_failures"][0]["count"] == 1
