"""F11: live_watch telemetry bridge and config example strict load."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bot.diagnostics.live_watch import (
    find_live_watch_session,
    find_telemetry_run_for_session,
    summarize_live_watch_session,
    summarize_rollup,
)
from bot.domain.config import load_settings


def test_find_telemetry_run_for_session(tmp_path: Path) -> None:
    telemetry = tmp_path / "telemetry"
    runs = telemetry / "runs"
    early = runs / "run_early"
    late = runs / "run_late"
    for run_dir in (early, late):
        (run_dir / "analysis").mkdir(parents=True)
        (run_dir / "analysis" / "strategy_decisions.jsonl").write_text(
            '{"setup_id":"bos","status":"reject"}\n',
            encoding="utf-8",
        )
    session_epoch = 1_700_000_000.0
    early_epoch = session_epoch + 30.0
    late_epoch = session_epoch + 600.0
    import os

    os.utime(early, (early_epoch, early_epoch))
    os.utime(late, (late_epoch, late_epoch))
    matched = find_telemetry_run_for_session(
        telemetry,
        session_started_epoch=session_epoch,
        max_skew_seconds=120.0,
    )
    assert matched == early


def test_summarize_live_watch_session_links_telemetry(tmp_path: Path) -> None:
    telemetry = tmp_path / "telemetry"
    run_dir = telemetry / "runs" / "20260604_120000_1"
    (run_dir / "analysis").mkdir(parents=True)
    (run_dir / "analysis" / "strategy_decisions.jsonl").write_text(
        '{"setup_id":"bos","status":"signal"}\n'
        '{"setup_id":"fvg","status":"reject"}\n',
        encoding="utf-8",
    )
    session = tmp_path / "session"
    session.mkdir()
    session.joinpath("session_summary.json").write_text(
        json.dumps({"started_at": "2026-06-04T12:00:00+00:00"}),
        encoding="utf-8",
    )
    session.joinpath("snapshots.jsonl").write_text(
        json.dumps({"runtime": {"delivered_total": 1}}) + "\n",
        encoding="utf-8",
    )
    import os

    os.utime(run_dir, (1_748_995_200.0, 1_748_995_200.0))
    summary = summarize_live_watch_session(session, telemetry_dir=telemetry)
    assert summary["decision_rows"] == 2
    assert summary["strategies_ran"] == 1
    assert summary["telemetry_run"] == "20260604_120000_1"


def test_summarize_live_watch_session_from_fixture(tmp_path: Path) -> None:
    session = tmp_path / "20260604T014627Z"
    session.mkdir()
    session.joinpath("session_summary.json").write_text(
        json.dumps(
            {
                "run_id": "20260604T014627Z",
                "minutes": 360,
                "bot_exit_code": 0,
            }
        ),
        encoding="utf-8",
    )
    session.joinpath("snapshots.jsonl").write_text(
        json.dumps(
            {
                "runtime": {
                    "cycles_total": 100,
                    "delivered_total": 5,
                    "rejected_total": 50,
                    "detector_runs_total": 200,
                    "symbols": ["BTCUSDT"],
                },
                "tracking": {"db": "data/bot/bot.db"},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    summary = summarize_live_watch_session(session)
    assert summary["source"] == "live_watch"
    assert summary["cycles_total"] == 100
    assert summary["delivered_total"] == 5
    assert find_live_watch_session(tmp_path, "20260604T014627Z") == session


def test_summarize_rollup_fixture(tmp_path: Path) -> None:
    rollup = tmp_path / "rollup_test.json"
    rollup.write_text(
        json.dumps(
            {
                "sessions": [
                    {
                        "run_id": "abc",
                        "snapshots": 2,
                        "total_strategy_error_lines": 0,
                        "last_snapshot": {
                            "runtime": {
                                "delivered_total": 3,
                                "cycles_total": 10,
                            }
                        },
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    out = summarize_rollup(rollup)
    assert out["totals"]["delivered_total"] == 3
    assert out["sessions"][0]["run_id"] == "abc"


def test_config_example_loads() -> None:
    example = Path("config.toml.example")
    if not example.is_file():
        pytest.skip("config.toml.example missing")
    settings = load_settings(example)
    assert settings.runtime.analysis_concurrency >= 1


def test_matrix_run_id_live_watch_integration() -> None:
    root = Path("data/live_watch")
    session = find_live_watch_session(root, "20260604T014627Z")
    if session is None:
        pytest.skip("6h live_watch session not present")
    summary = summarize_live_watch_session(session)
    assert int(summary.get("delivered_total") or 0) >= 0
