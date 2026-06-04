"""Wave E4 — funnel normalization and confluence leg analytics."""

from __future__ import annotations

from bot.dashboard.live import DashboardLiveData
from bot.dashboard.user_summary import build_funnel_hint
from bot.domain.labels import normalize_reject_reason, reject_reason_ru


def test_rejections_aggregate_normalized_reasons() -> None:
    live = DashboardLiveData(lambda: None)

    def _fake_iter(_stem: str, *, max_rows: int, limit_files: int):  # noqa: ARG001
        yield {
            "reason": "hard_confluence_gate_failed",
            "stage": "confluence",
            "setup_id": "ema_bounce",
            "symbol": "BTCUSDT",
        }
        yield {
            "reason": "LIMIT_SETUP_INVALIDATED",
            "stage": "limit_entry",
            "setup_id": "fvg_setup",
            "symbol": "ETHUSDT",
        }
        yield {
            "reason": "htf_reversal_conflict:1h,4h",
            "stage": "filters",
            "setup_id": "rsi_div",
            "symbol": "SOLUSDT",
        }

    live._iter_recent = _fake_iter  # type: ignore[method-assign]
    payload = live._rejections_uncached(limit=10, max_rows=100)

    keys = {row["key"] for row in payload["reasons"]}
    assert keys == {"hard_confluence_gate", "limit_publish_rejected", "htf_reversal_conflict"}
    assert payload["reasons"][0]["label_ru"] == reject_reason_ru(payload["reasons"][0]["key"])
    assert payload["reasons"][0]["example"].get("raw_reason") == "hard_confluence_gate_failed"


def test_confluence_legs_count_failed_confirmations() -> None:
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
        }
        yield {
            "stage": "confluence",
            "reason": "htf_reversal_conflict",
            "confirmations": {
                "trend": True,
                "momentum": False,
                "volume": True,
                "htf": False,
                "microstructure": False,
            },
        }
        yield {
            "stage": "filters",
            "reason": "score_too_low",
        }

    live._iter_recent = _fake_iter  # type: ignore[method-assign]
    payload = live._confluence_legs_uncached(max_rows=100)

    assert payload["gate_rejects"] == 2
    counts = {row["key"]: row["count"] for row in payload["leg_failures"]}
    assert counts["trend"] == 1
    assert counts["momentum"] == 1
    assert counts["volume"] == 1
    assert counts["htf"] == 2
    assert counts["microstructure"] == 1
    assert payload["total_leg_failures"] == 6


def test_funnel_combined_reject_hint_prefers_delivery() -> None:
    hint = build_funnel_hint(
        overview={"top_rejection": {"key": "spread_too_wide", "count": 5, "label_ru": "широкий спред"}},
        funnel={
            "cycle_totals": {"candidates": 3, "delivered": 0},
            "combined_reject_hint": {
                "source": "strategy_decisions",
                "key": "pattern.no_hit",
                "count": 99,
                "label_ru": "pattern no hit",
            },
        },
    )
    assert hint["top_filter"] == "spread_too_wide"
    assert hint["top_filter_ru"] == "широкий спред"


def test_funnel_hint_falls_back_to_decision_rejects() -> None:
    hint = build_funnel_hint(
        overview={},
        funnel={
            "cycle_totals": {"candidates": 2, "delivered": 0},
            "combined_reject_hint": {
                "source": "strategy_decisions",
                "key": "atr_too_low",
                "count": 4,
                "label_ru": reject_reason_ru("atr_too_low"),
            },
        },
    )
    assert hint["top_filter"] == "atr_too_low"
    assert hint["top_filter_ru"] == reject_reason_ru("atr_too_low")
    assert "atr" in hint["text"].lower() or "ATR" in hint["text"]


def test_normalize_filter_reject_variants() -> None:
    assert normalize_reject_reason("stale_15m") == "stale_15m"
    assert normalize_reject_reason("5m_opposes_long") == "5m_opposes_long"
    assert reject_reason_ru("filter_pipeline_crash") == "сбой фильтра"
    assert reject_reason_ru("r_class_action_blocked") == "R-класс только WATCH"
