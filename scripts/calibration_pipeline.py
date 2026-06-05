#!/usr/bin/env python3
"""Ops calibration pipeline — shortlist matrix, defaults reconcile, DB status.

Writes consolidated artifacts under data/bot/reports/.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

try:
    from scripts.common import bootstrap_repo_path
except ModuleNotFoundError:
    from common import bootstrap_repo_path

bootstrap_repo_path()

from bot.diagnostics.facade import build_zero_hit_triage
from bot.domain.config import load_settings
from bot.persistence.db_status import collect_db_status
from scripts.reconcile_strategy_defaults import (
    collect_defaults_drift,
    write_drift_report,
    write_toml_patch,
)

LOG = logging.getLogger("scripts.calibration_pipeline")

REPORTS_DIR = Path("data/bot/reports")


def _run_subprocess(cmd: list[str]) -> tuple[int, str, str]:
    proc = subprocess.run(cmd, check=False, capture_output=True, text=True)
    return proc.returncode, proc.stdout, proc.stderr


def run_shortlist_matrix(
    *,
    config: Path,
    output: Path,
    static: bool = True,
    run_id: str = "",
    live_watch_dir: Path | None = None,
) -> dict[str, Any]:
    cmd = [
        sys.executable,
        "scripts/strategy_shortlist_matrix.py",
        "--config",
        str(config),
        "--json",
    ]
    if static:
        cmd.append("--static")
    if run_id:
        cmd.extend(["--run-id", run_id])
    if live_watch_dir is not None:
        cmd.extend(["--live-watch-dir", str(live_watch_dir)])
    LOG.info("running shortlist matrix | cmd=%s", " ".join(cmd))
    code, stdout, stderr = _run_subprocess(cmd)
    result: dict[str, Any] = {"exit_code": code, "stderr_tail": (stderr or "")[-500:]}
    if code != 0:
        return result
    payload = json.loads(stdout) if stdout.strip() else {}
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    result["output"] = str(output)
    result["static_rows"] = len(payload.get("static", []))
    if payload.get("live_telemetry"):
        lt = payload["live_telemetry"]
        result["telemetry_source"] = payload.get("telemetry_source")
        result["delivered_total"] = lt.get("delivered_total")
        result["cycles_total"] = lt.get("cycles_total") or lt.get("decision_rows")
    return result


async def collect_db_status_report(*, config: Path, output: Path) -> dict[str, Any]:
    settings = load_settings(config)
    summary = await collect_db_status(settings)
    payload: dict[str, Any] = {
        "generated_at": datetime.now(UTC).isoformat(),
        "db_path": str(settings.db_path),
        "migration_version": summary.migration_version,
        "migrations": [
            {"version": version, "description": description, "applied_at": applied_at}
            for version, description, applied_at in summary.migrations
        ],
        "signal_counts": dict(summary.signal_counts),
        "outcome_counts": dict(summary.outcome_counts),
        "outcomes_total": summary.outcomes_total,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return payload


def run_reconcile_defaults(
    *,
    config_dir: Path,
    drift_output: Path,
    patch_output: Path,
    report_only: bool,
) -> dict[str, Any]:
    rows = collect_defaults_drift(config_dir=config_dir)
    drift_payload = write_drift_report(rows, output=drift_output)
    patch_path = write_toml_patch(rows, output=patch_output)
    drift_count = int(drift_payload["summary"]["drift"])
    exit_code = 0 if report_only or drift_count == 0 else 1
    return {
        "exit_code": exit_code,
        "drift": drift_count,
        "drift_report": str(drift_output),
        "patch": str(patch_path),
    }


async def run_calibration_pipeline(
    *,
    config: Path,
    reports_dir: Path,
    reconcile_report_only: bool = True,
    static_matrix: bool = True,
    run_id: str = "",
    live_watch_dir: Path | None = None,
) -> dict[str, Any]:
    reports_dir.mkdir(parents=True, exist_ok=True)
    matrix_output = reports_dir / "shortlist_matrix.json"
    drift_output = reports_dir / "strategy_defaults_drift.json"
    patch_output = reports_dir / "config_strategies.toml.patch"
    db_output = reports_dir / "db_status.json"

    matrix_result = run_shortlist_matrix(
        config=config,
        output=matrix_output,
        static=static_matrix,
        run_id=run_id,
        live_watch_dir=live_watch_dir,
    )
    zero_hit_path = reports_dir / "zero_hit_triage.json"
    if matrix_output.is_file():
        matrix_payload = json.loads(matrix_output.read_text(encoding="utf-8"))
        triage = build_zero_hit_triage(matrix_payload.get("live_telemetry"))
        triage["run_id"] = matrix_payload.get("run_id") or run_id
        triage["telemetry_source"] = matrix_payload.get("telemetry_source")
        zero_hit_path.write_text(json.dumps(triage, indent=2, sort_keys=True), encoding="utf-8")
        matrix_result["zero_hit_triage"] = str(zero_hit_path)
        matrix_result["zero_run_count"] = len(triage.get("zero_runs") or [])
    reconcile_result = run_reconcile_defaults(
        config_dir=Path("config/strategies"),
        drift_output=drift_output,
        patch_output=patch_output,
        report_only=reconcile_report_only,
    )
    db_payload = await collect_db_status_report(config=config, output=db_output)

    summary: dict[str, Any] = {
        "generated_at": datetime.now(UTC).isoformat(),
        "reports_dir": str(reports_dir),
        "matrix": matrix_result,
        "reconcile": reconcile_result,
        "db_status": {
            "output": str(db_output),
            "migration_version": db_payload["migration_version"],
            "outcomes_total": db_payload["outcomes_total"],
        },
    }
    summary_path = reports_dir / "calibration_pipeline_summary.json"
    summary_path.write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")
    summary["summary_path"] = str(summary_path)
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("config.toml"))
    parser.add_argument(
        "--reports-dir",
        type=Path,
        default=REPORTS_DIR,
        help="Directory for matrix/reconcile/db_status artifacts",
    )
    parser.add_argument(
        "--fail-on-drift",
        action="store_true",
        help="Exit 1 when reconcile detects defaults drift (default: report-only)",
    )
    parser.add_argument(
        "--live-matrix",
        action="store_true",
        help="Run live shortlist matrix instead of static theory table",
    )
    parser.add_argument(
        "--run-id",
        type=str,
        default="",
        help="Telemetry or live_watch session id for matrix slice",
    )
    parser.add_argument(
        "--live-watch-dir",
        type=Path,
        default=Path("data/live_watch"),
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Run calibration even when BOT_ALLOW_CALIBRATION is unset (post-architecture wave)",
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")

    if not args.force and os.getenv("BOT_ALLOW_CALIBRATION", "").strip().lower() not in (
        "1",
        "true",
        "yes",
    ):
        LOG.error(
            "calibration blocked: run after W1–W3 stabilize. "
            "Set BOT_ALLOW_CALIBRATION=1 or pass --force."
        )
        return 2

    summary = asyncio.run(
        run_calibration_pipeline(
            config=args.config,
            reports_dir=args.reports_dir,
            reconcile_report_only=not args.fail_on_drift,
            static_matrix=not args.live_matrix,
            run_id=args.run_id.strip(),
            live_watch_dir=args.live_watch_dir,
        )
    )
    LOG.info("calibration pipeline complete | summary=%s", summary.get("summary_path"))
    matrix_code = int(summary["matrix"].get("exit_code", 0))
    reconcile_code = int(summary["reconcile"].get("exit_code", 0))
    if matrix_code != 0:
        return matrix_code
    if reconcile_code != 0:
        return reconcile_code
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
