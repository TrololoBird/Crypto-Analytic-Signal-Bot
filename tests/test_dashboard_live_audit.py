"""Unit tests for slim dashboard live audit snapshot."""

from __future__ import annotations

from bot.dashboard.live_audit import audit_snapshot, build_dashboard_audit_snapshot


def _empty_snapshot(**overrides: object) -> dict:
    base = build_dashboard_audit_snapshot(
        overview={"running": True},
        funnel={"cycle_totals": {"cycles": 0}},
        shortlist={"total": 50, "dynamic": 50, "zero_fit": 0, "source": "rest_full"},
        decisions={"total_rows": 10, "status_counts": {"signal": 2}},
        rejections={"total_rows": 0, "reasons": []},
        delivery={"selected_count": 1, "delivery_count": 1},
        runtime={"ws_snapshot": {"fresh_tickers": 10, "fresh_mark_prices": 10}},
        telegram={"available": True, "preview": {"ok": True, "chars": 500}},
    )
    base.update(overrides)
    return base


def test_audit_snapshot_healthy_minimal() -> None:
    report = audit_snapshot(_empty_snapshot())
    assert report["status"] == "healthy"
    assert report["score"] == 100
    assert "generated_at" in report
    assert "operator_brief" in report
    assert isinstance(report["findings"], list)
    assert isinstance(report["action_plan"], list)


def test_audit_snapshot_bot_not_running() -> None:
    snap = _empty_snapshot()
    snap["overview"] = {"running": False}
    report = audit_snapshot(snap)
    assert report["status"] == "critical"
    codes = {row["code"] for row in report["findings"]}
    assert "bot_not_running" in codes


def test_audit_snapshot_funnel_zero_signals() -> None:
    snap = _empty_snapshot()
    snap["funnel"] = {
        "cycle_totals": {"cycles": 5, "detector_runs": 100, "candidates": 0, "delivered": 0},
        "decisions": {"signal_rate": 0.0},
    }
    report = audit_snapshot(snap)
    codes = {row["code"] for row in report["findings"]}
    assert "zero_raw_signal_rate" in codes
    assert "zero_post_filter_candidates" in codes
