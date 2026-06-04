"""Wave F9 Agent U: CLI --config, Makefile ops targets, supervisor config hook."""

from __future__ import annotations

from pathlib import Path

from bot.cli import _resolve_config_path, build_parser
from scripts.agent_bot_supervisor import _build_parser as build_supervisor_parser


def test_cli_default_config() -> None:
    args = build_parser().parse_args([])
    assert _resolve_config_path(args) == Path("config.toml")
    assert args.command is None


def test_cli_run_subcommand_config() -> None:
    args = build_parser().parse_args(["run", "--config", "custom/config.toml"])
    assert args.command == "run"
    assert _resolve_config_path(args) == Path("custom/config.toml")


def test_cli_status_subcommand_config() -> None:
    args = build_parser().parse_args(["status", "--config", "ops/config.toml"])
    assert args.command == "status"
    assert _resolve_config_path(args) == Path("ops/config.toml")


def test_cli_stop_subcommand_config() -> None:
    args = build_parser().parse_args(["stop", "--config", "staging.toml"])
    assert args.command == "stop"
    assert _resolve_config_path(args) == Path("staging.toml")


def test_cli_db_migrate_subcommand_config() -> None:
    args = build_parser().parse_args(["db", "migrate", "--config", "prod/config.toml"])
    assert args.command == "db"
    assert args.db_command == "migrate"
    assert _resolve_config_path(args) == Path("prod/config.toml")


def test_cli_top_level_config_for_default_run() -> None:
    args = build_parser().parse_args(["--config", "root/config.toml"])
    assert args.command is None
    assert _resolve_config_path(args) == Path("root/config.toml")


def test_makefile_ops_targets_present() -> None:
    repo = Path(__file__).resolve().parents[1]
    makefile = (repo / "Makefile").read_text(encoding="utf-8")
    for target in (
        "nightly-calibration:",
        "reconcile-defaults:",
        "shortlist-matrix:",
        "graphify-update:",
    ):
        assert target in makefile
    assert "nightly_strategy_calibration.py" in makefile
    assert "reconcile_strategy_defaults.py" in makefile
    assert "strategy_shortlist_matrix.py" in makefile


def test_supervisor_config_and_calibration_note_flags() -> None:
    args = build_supervisor_parser().parse_args(
        ["--config", "custom/config.toml", "--calibration-note"]
    )
    assert args.config == Path("custom/config.toml")
    assert args.calibration_note is True


def test_supervisor_default_config() -> None:
    args = build_supervisor_parser().parse_args([])
    assert args.config == Path("config.toml")
    assert args.calibration_note is False
