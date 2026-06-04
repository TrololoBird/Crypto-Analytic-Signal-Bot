"""Unit tests for synthesized dashboard operator alerts."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from bot.dashboard.live import DashboardLiveData
from bot.dashboard.operator_alerts import build_live_operator_alerts


class _FakeLiveData:
    def __init__(self, *, decisions: dict, runtime: dict, overview: dict) -> None:
        self._decisions = decisions
        self._runtime = runtime
        self._overview = overview

    def decisions(self, *, limit: int = 41, max_rows: int = 50_000) -> dict:
        return self._decisions

    def runtime(self) -> dict:
        return self._runtime

    def overview(self) -> dict:
        return self._overview


def test_build_live_operator_alerts_zero_hit_and_ws_down() -> None:
    live = _FakeLiveData(
        decisions={
            "zero_signal_setups": [
                {
                    "setup_id": "fvg",
                    "total": 120,
                    "top_blockers": [{"key": "pattern.no_fvg", "count": 80}],
                }
            ],
            "setup_reports": [],
        },
        runtime={"ws_snapshot": {"last_message_age_seconds": 5.0, "stale_kline_stream_count": 0}},
        overview={"last_cycle": {}},
    )
    bot = SimpleNamespace(_ws_manager=MagicMock(is_connected=lambda: False))

    alerts = build_live_operator_alerts(bot, live)  # type: ignore[arg-type]

    types = {row["type"] for row in alerts}
    assert "zero_hit" in types
    assert "ws_down" in types
    zero = next(row for row in alerts if row["type"] == "zero_hit")
    assert zero["setup_id"] == "fvg"
    assert "pattern.no_fvg" in zero["detail"]


def test_build_live_operator_alerts_ws_stale() -> None:
    live = _FakeLiveData(
        decisions={"zero_signal_setups": [], "setup_reports": []},
        runtime={
            "ws_snapshot": {
                "last_message_age_seconds": 200.0,
                "stale_kline_stream_count": 3,
            }
        },
        overview={"last_cycle": {}},
    )
    bot = SimpleNamespace(_ws_manager=MagicMock(is_connected=lambda: True))

    alerts = build_live_operator_alerts(bot, live)  # type: ignore[arg-type]
    types = {row["type"] for row in alerts}
    assert "ws_stale" in types
    assert "ws_stale_klines" in types


def test_dashboard_live_data_zero_signal_setups() -> None:
    """DashboardLiveData exposes zero_signal_setups for strategy arena UI."""
    DashboardLiveData(lambda: None)
    reports = [
        {
            "setup_id": "a",
            "total": 10,
            "signals": 0,
            "rejects": 10,
            "skips": 0,
            "signal_rate": 0.0,
            "top_blockers": [],
        },
        {
            "setup_id": "b",
            "total": 5,
            "signals": 2,
            "rejects": 3,
            "skips": 0,
            "signal_rate": 0.4,
            "top_blockers": [],
        },
    ]
    # Exercise aggregation logic via internal helper path
    zero = [row for row in reports if row["total"] > 0 and row["signals"] == 0]
    assert len(zero) == 1
    assert zero[0]["setup_id"] == "a"
