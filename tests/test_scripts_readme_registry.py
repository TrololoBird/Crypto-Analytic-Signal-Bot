from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from scripts.live_runtime_monitor import RuntimeMonitor
from scripts.check_scripts_readme import _listed_scripts

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        (
            (
                "| `live_smoke_bot.py` | Smoke test | active | --warmup | "
                "`python scripts/live_smoke_bot.py` |"
            ),
            {"live_smoke_bot.py"},
        ),
        (
            (
                "| live_smoke_bot.py | missing backticks | active | --warmup | "
                "`python scripts/live_smoke_bot.py` |"
            ),
            set(),
        ),
        (
            "| `README.md` | not a python script row | active | - | - |",
            set(),
        ),
        (
            "| malformed row without separators",
            set(),
        ),
    ],
)
def test_listed_scripts_parses_table_rows(line: str, expected: set[str]) -> None:
    assert _listed_scripts(line) == expected


def test_scripts_readme_inventory_is_synced() -> None:
    result = subprocess.run(
        [sys.executable, "scripts/check_scripts_readme.py"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, (
        "scripts/README.md is out of sync with scripts/*.py\n"
        f"stdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )


def test_validate_config_exposes_documented_cli_options() -> None:
    result = subprocess.run(
        [sys.executable, "scripts/validate_config.py", "--help"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert "--config" in result.stdout
    assert "--symbol" in result.stdout
    assert "--interval" in result.stdout
    assert "--limit" in result.stdout


def test_live_runtime_monitor_exposes_documented_cli_options() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "scripts.live_runtime_monitor", "--help"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert "--poll-interval" in result.stdout
    assert "--log-dir" in result.stdout


def test_live_check_binance_api_supports_module_help() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "scripts.live_check_binance_api", "--help"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert "--warmup-seconds" in result.stdout
    assert "--reconnect-wait-seconds" in result.stdout


def test_live_runtime_monitor_summary_is_ascii_safe(capsys: pytest.CaptureFixture[str]) -> None:
    report = {
        "monitoring_session": {"duration_seconds": 0.0},
        "statistics": {
            "total_cycles": 0,
            "unique_symbols_processed": 0,
            "detector_runs_total": 0,
            "candidates_total": 0,
            "delivered_total": 0,
            "rejected_total": 0,
            "errors_count": 0,
        },
        "performance": {
            "cycles_per_minute": 0.0,
            "candidates_per_cycle": 0.0,
            "delivery_rate": 0.0,
        },
        "signals": {"last_signals": []},
        "errors_sample": [],
        "report_path": "scripts/audit_data/runtime_report_test.json",
    }

    RuntimeMonitor()._print_summary(report)

    captured = capsys.readouterr()
    captured.out.encode("ascii")
