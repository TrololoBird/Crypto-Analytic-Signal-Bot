"""Wave F9 agent Q — routed signal rate, lane rejections, audit health score."""

from __future__ import annotations

from bot.dashboard.live import DashboardLiveData, _is_routing_excluded_decision_reason
from bot.dashboard.live_audit import audit_snapshot, build_dashboard_audit_snapshot
from bot.domain.labels import normalize_reject_reason


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


def test_lane_expected_rejection_normalizes_shortlist_not_routed() -> None:
    snap = _empty_snapshot()
    snap["rejections"] = {
        "total_rows": 100,
        "reasons": [{"key": "asset_fit.shortlist_not_routed", "count": 80}],
    }
    report = audit_snapshot(snap)
    dominant = next(row for row in report["findings"] if row["code"] == "dominant_rejection")
    assert dominant["severity"] == "info"
    assert normalize_reject_reason(dominant["evidence"]["reason"]) == "shortlist_not_routed"


def test_lane_expected_rejection_accepts_normalized_key() -> None:
    snap = _empty_snapshot()
    snap["rejections"] = {
        "total_rows": 50,
        "reasons": [{"key": "shortlist_not_routed", "count": 40}],
    }
    report = audit_snapshot(snap)
    dominant = next(row for row in report["findings"] if row["code"] == "dominant_rejection")
    assert dominant["severity"] == "info"


def test_zero_raw_signal_rate_uses_routed_signal_rate() -> None:
    snap = _empty_snapshot()
    snap["funnel"] = {
        "cycle_totals": {"cycles": 5, "detector_runs": 100, "candidates": 0, "delivered": 0},
        "decisions": {
            "signal_rate": 0.0,
            "routed_signal_rate": 0.25,
        },
    }
    report = audit_snapshot(snap)
    codes = {row["code"] for row in report["findings"]}
    assert "zero_raw_signal_rate" not in codes


def test_zero_raw_signal_rate_fires_when_routed_rate_zero() -> None:
    snap = _empty_snapshot()
    snap["funnel"] = {
        "cycle_totals": {"cycles": 5, "detector_runs": 100, "candidates": 0, "delivered": 0},
        "decisions": {
            "signal_rate": 0.0,
            "routed_signal_rate": 0.0,
        },
    }
    report = audit_snapshot(snap)
    finding = next(row for row in report["findings"] if row["code"] == "zero_raw_signal_rate")
    assert finding["evidence"]["routed_signal_rate"] == 0.0


def test_health_score_ignores_info_findings() -> None:
    snap = _empty_snapshot()
    snap["shortlist"] = {"total": 50, "dynamic": 50, "zero_fit": 0, "source": "cached"}
    snap["overview"] = {
        "running": True,
        "top_rejection": {"key": "pattern.no_hit", "count": 1},
    }
    report = audit_snapshot(snap)
    assert report["summary"]["info"] >= 2
    assert report["summary"]["warning"] == 0
    assert report["summary"]["critical"] == 0
    assert report["score"] == 100


def test_routing_excluded_decision_reason() -> None:
    assert _is_routing_excluded_decision_reason("runtime.strategy_lane_excluded")
    assert _is_routing_excluded_decision_reason("asset_fit.shortlist_not_routed")
    assert _is_routing_excluded_decision_reason("asset_fit.liquidity_rank_too_low")
    assert not _is_routing_excluded_decision_reason("pattern.no_hit")
    assert not _is_routing_excluded_decision_reason("data.insufficient_input")


def test_decisions_routed_signal_rate_excludes_routing_skips() -> None:
    live = DashboardLiveData(lambda: None)

    def _fake_iter(stem: str, *, max_rows: int, limit_files: int):  # noqa: ARG001
        if stem != "strategy_decisions":
            return
        yield {"status": "skip", "reason_code": "runtime.strategy_lane_excluded", "setup_id": "a"}
        yield {"status": "skip", "reason_code": "asset_fit.shortlist_not_routed", "setup_id": "b"}
        yield {"status": "reject", "reason_code": "pattern.no_hit", "setup_id": "c"}
        yield {"status": "signal", "reason_code": "pattern.hit", "setup_id": "d"}

    live._iter_recent = _fake_iter  # type: ignore[method-assign]
    payload = live._decisions_uncached(limit=10, max_rows=100)

    assert payload["total_rows"] == 4
    assert payload["routed_total_rows"] == 2
    assert payload["signal_rate"] == 0.25
    assert payload["routed_signal_rate"] == 0.5
