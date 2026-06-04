"""F11: live_watch telemetry bridge and config example strict load."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bot.diagnostics.live_watch import (
    find_live_watch_session,
    summarize_live_watch_session,
    summarize_rollup,
)
from bot.domain.config import load_settings


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
