"""CLI for joining dashboard outcome stats with live strategy surface checks."""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path
from typing import Any

from bot.diagnostics.runtime.strategy_audit import (
    SCHEDULED_SETUP_IDS,
    build_audit_report,
    gate_failures,
    print_report,
    summarize_actions,
    write_report_json,
)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Audit all registered strategy surfaces")
    parser.add_argument("--db", default="data/bot/bot.db")
    parser.add_argument(
        "--summary-json",
        default="data/bot/telemetry/strategy_prompt_baseline.json",
        help="live_check_strategies summary JSON to join with SQLite outcomes",
    )
    parser.add_argument(
        "--decisions-jsonl",
        default="",
        help="Optional strategy_decisions.jsonl; pass 'latest' to use the newest telemetry run.",
    )
    parser.add_argument("--config", default="config.toml")
    parser.add_argument("--last-days", type=int, default=90)
    parser.add_argument("--write-json", default="")
    parser.add_argument("--actions-only", action="store_true")
    parser.add_argument("--require-registered", type=int, default=38)
    parser.add_argument("--require-score-differentiation", action="store_true")
    parser.add_argument("--require-signal-contract", action="store_true")
    parser.add_argument("--require-no-zero-signals", action="store_true")
    parser.add_argument(
        "--allow-scheduled-zero",
        nargs="*",
        default=sorted(SCHEDULED_SETUP_IDS),
    )
    parser.add_argument("--min-python-lines", type=int, default=0)
    parser.add_argument(
        "--print-json",
        action="store_true",
        help="Print machine-readable report instead of table output",
    )
    return parser.parse_args()


def _python_lines_changed() -> int:
    try:
        proc = subprocess.run(
            ["git", "diff", "--numstat", "HEAD", "--", "*.py"],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        return 0
    if proc.returncode not in (0, 1):
        return 0
    total = 0
    for line in proc.stdout.splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        added, deleted, path = parts[0], parts[1], parts[2]
        if not path.endswith(".py"):
            continue
        for value in (added, deleted):
            if value == "-":
                continue
            try:
                total += int(value)
            except ValueError:
                continue
    return total


def _compact_action_summary(actions: dict[str, list[str]]) -> list[dict[str, Any]]:
    return [
        {
            "action": action,
            "count": len(setups),
            "setups": setups,
        }
        for action, setups in actions.items()
    ]


def main() -> None:
    args = _parse_args()
    report = build_audit_report(
        db_path=args.db,
        summary_path=args.summary_json,
        decisions_path=args.decisions_jsonl or None,
        config_path=args.config,
        last_days=args.last_days,
    )
    py_lines = _python_lines_changed()
    failures = gate_failures(
        report,
        min_py_lines=args.min_python_lines or None,
        py_lines_changed=py_lines if args.min_python_lines else None,
        require_registered=args.require_registered,
        require_score_differentiation=bool(args.require_score_differentiation),
        require_signal_contract=bool(args.require_signal_contract),
        allow_scheduled_zero=args.allow_scheduled_zero,
    )
    if not args.require_no_zero_signals:
        failures = [
            failure for failure in failures if not failure.startswith("zero_signal_setups:")
        ]
    payload = report.to_dict()
    payload["python_lines_changed"] = py_lines
    payload["gate_failures"] = failures
    payload["action_summary"] = _compact_action_summary(summarize_actions(report.rows))
    if args.write_json:
        write_report_json(report, args.write_json)
        path = Path(args.write_json)
        merged = json.loads(path.read_text(encoding="utf-8"))
        merged["python_lines_changed"] = py_lines
        merged["gate_failures"] = failures
        merged["action_summary"] = payload["action_summary"]
        path.write_text(json.dumps(merged, indent=2, sort_keys=True), encoding="utf-8")
    if args.print_json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print_report(report, include_all=not args.actions_only)
        print(f"python_lines_changed={py_lines}")
        print(f"gate_failures={failures}")
        print(f"action_summary={payload['action_summary']}")
    if failures:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
