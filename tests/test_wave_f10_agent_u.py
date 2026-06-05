"""Wave F10 Agent U — calibration pipeline, reconcile patch, outcomes CLI, runtime errors."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from bot.cli import build_parser
from bot.persistence.db_status import DbStatusSummary
from bot.runtime.errors import (
    DEFENSIVE_EXC,
    build_runtime_error_payload,
    classify_runtime_error,
)
from scripts.calibration_pipeline import run_calibration_pipeline, run_reconcile_defaults
from scripts.reconcile_strategy_defaults import (
    collect_defaults_drift,
    write_toml_patch,
)
from scripts.reconcile_strategy_defaults import (
    main as reconcile_main,
)


def test_runtime_errors_live_under_bot_runtime() -> None:
    assert classify_runtime_error(KeyError("x")) == "schema"
    assert ValueError in DEFENSIVE_EXC
    payload = build_runtime_error_payload(component="test", exc=RuntimeError("boom"))
    assert payload["component"] == "test"
    assert not Path("bot/core/runtime_errors.py").exists()


def test_cli_outcomes_subcommand_and_backtest_alias_help() -> None:
    parser = build_parser()
    outcomes = parser.parse_args(["outcomes", "--days", "7", "--setup", "order_block"])
    backtest = parser.parse_args(["backtest", "--days", "14"])
    assert outcomes.command == "outcomes"
    assert outcomes.days == 7
    assert outcomes.setup == "order_block"
    assert backtest.command == "backtest"
    assert backtest.days == 14
    help_text = parser.format_help()
    assert "outcomes" in help_text
    assert "Alias for outcomes" in help_text


def test_reconcile_detects_min_rr_and_sl_buffer_drift(tmp_path: Path) -> None:
    config_dir = tmp_path / "strategies"
    config_dir.mkdir()
    toml = config_dir / "order_block.toml"
    toml.write_text(
        """
[strategy]
name = "order_block"

[risk_management]
sl_buffer_atr = 9.99
min_rr = 0.5

[scoring]
base_score = 0.52
""".strip(),
        encoding="utf-8",
    )
    rows = collect_defaults_drift(config_dir=config_dir)
    by_field = {(row.setup_id, row.field): row for row in rows if row.setup_id == "order_block"}
    assert by_field[("order_block", "min_rr")].status == "drift"
    assert by_field[("order_block", "sl_buffer_atr")].status == "drift"
    assert by_field[("order_block", "min_rr")].code_value == pytest.approx(1.9)
    assert by_field[("order_block", "sl_buffer_atr")].code_value == pytest.approx(0.5)


def test_reconcile_writes_patch_and_exits_on_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_dir = tmp_path / "strategies"
    config_dir.mkdir()
    (config_dir / "order_block.toml").write_text(
        """
[strategy]
name = "order_block"
[risk_management]
sl_buffer_atr = 9.0
min_rr = 1.9
[scoring]
base_score = 0.52
""".strip(),
        encoding="utf-8",
    )
    drift_out = tmp_path / "drift.json"
    patch_out = tmp_path / "patch.toml"
    rows = collect_defaults_drift(config_dir=config_dir)
    write_toml_patch(rows, output=patch_out)
    assert patch_out.exists()
    patch_text = patch_out.read_text(encoding="utf-8")
    assert "sl_buffer_atr" in patch_text
    assert "order_block" in patch_text

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "reconcile_strategy_defaults",
            "--config-dir",
            str(config_dir),
            "--output",
            str(drift_out),
            "--patch-output",
            str(patch_out),
        ],
    )
    assert reconcile_main() == 1

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "reconcile_strategy_defaults",
            "--report-only",
            "--config-dir",
            str(config_dir),
            "--output",
            str(drift_out),
            "--patch-output",
            str(patch_out),
        ],
    )
    assert reconcile_main() == 0


@pytest.mark.asyncio
async def test_calibration_pipeline_writes_reports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reports_dir = tmp_path / "reports"
    matrix_payload = {"static": [{"setup_id": "order_block", "fit_score": 4}]}

    def fake_matrix(*_args: object, **_kwargs: object) -> dict[str, object]:
        out = reports_dir / "shortlist_matrix.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(matrix_payload), encoding="utf-8")
        return {"exit_code": 0, "output": str(out), "static_rows": 1}

    monkeypatch.setattr(
        "scripts.calibration_pipeline.run_shortlist_matrix",
        fake_matrix,
    )

    db_summary = DbStatusSummary(
        migration_version=3,
        migrations=[(1, "init", "2026-01-01")],
        signal_counts={"active": 1},
        outcome_counts={"win": 5, "loss": 7},
    )

    async def fake_db_report(*, config: Path, output: Path) -> dict[str, object]:
        del config
        payload = {
            "generated_at": "2026-01-01T00:00:00+00:00",
            "db_path": "data/bot/bot.db",
            "migration_version": db_summary.migration_version,
            "migrations": [],
            "signal_counts": dict(db_summary.signal_counts),
            "outcome_counts": dict(db_summary.outcome_counts),
            "outcomes_total": db_summary.outcomes_total,
        }
        await asyncio.to_thread(output.write_text, json.dumps(payload), encoding="utf-8")
        return payload

    with patch(
        "scripts.calibration_pipeline.collect_db_status_report",
        new=fake_db_report,
    ):
        summary = await run_calibration_pipeline(
            config=Path("config.toml"),
            reports_dir=reports_dir,
            reconcile_report_only=True,
        )
    assert (reports_dir / "shortlist_matrix.json").exists()
    assert (reports_dir / "strategy_defaults_drift.json").exists()
    assert (reports_dir / "config_strategies.toml.patch").exists()
    assert (reports_dir / "db_status.json").exists()
    assert (reports_dir / "calibration_pipeline_summary.json").exists()
    assert summary["matrix"]["exit_code"] == 0
    assert summary["db_status"]["outcomes_total"] == 12  # win + loss


def test_run_reconcile_defaults_exit_code(tmp_path: Path) -> None:
    config_dir = tmp_path / "strategies"
    config_dir.mkdir()
    (config_dir / "order_block.toml").write_text(
        """
[strategy]
name = "order_block"
[risk_management]
sl_buffer_atr = 0.5
min_rr = 1.9
[scoring]
base_score = 0.52
""".strip(),
        encoding="utf-8",
    )
    result = run_reconcile_defaults(
        config_dir=config_dir,
        drift_output=tmp_path / "drift.json",
        patch_output=tmp_path / "patch.toml",
        report_only=False,
    )
    assert result["exit_code"] == 0


def test_makefile_calibration_pipeline_target() -> None:
    repo = Path(__file__).resolve().parents[1]
    makefile = (repo / "Makefile").read_text(encoding="utf-8")
    assert "calibration-pipeline:" in makefile
    assert "calibration_pipeline.py" in makefile
