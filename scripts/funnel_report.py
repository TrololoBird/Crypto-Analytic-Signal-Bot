#!/usr/bin/env python3
"""Summarize funnel telemetry for the latest (or specified) bot run."""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

try:
    from scripts.common import bootstrap_repo_path, configure_script_logging
except ModuleNotFoundError:  # pragma: no cover
    from common import bootstrap_repo_path, configure_script_logging

from bot.domain.config import load_settings

LOG = configure_script_logging("scripts.funnel_report")


def _iter_jsonl(path: Path):
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            yield json.loads(line)
        except json.JSONDecodeError:
            continue


def _latest_run_dir(runs_dir: Path) -> Path | None:
    if not runs_dir.exists():
        return None
    dirs = sorted(
        (p for p in runs_dir.iterdir() if p.is_dir()),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return dirs[0] if dirs else None


def _count_rejection_reasons(analysis_dir: Path) -> Counter[str]:
    counts: Counter[str] = Counter()
    for row in _iter_jsonl(analysis_dir / "rejected.jsonl"):
        reason = str(row.get("reason") or row.get("reject_reason") or "unknown")
        counts[reason] += 1
    for row in _iter_jsonl(analysis_dir / "strategy_decisions.jsonl"):
        if str(row.get("decision") or "").lower() not in {"reject", "rejected"}:
            continue
        reason = str(row.get("reason") or row.get("reject_reason") or "unknown")
        counts[reason] += 1
    return counts


def build_report(*, analysis_dir: Path, run_id: str) -> dict[str, object]:
    cycles = list(_iter_jsonl(analysis_dir / "cycles.jsonl"))
    candidates = list(_iter_jsonl(analysis_dir / "candidates.jsonl"))
    selected = list(_iter_jsonl(analysis_dir / "selected.jsonl"))
    delivery = list(_iter_jsonl(analysis_dir / "delivery.jsonl"))
    mismatches = list(_iter_jsonl(analysis_dir / "telemetry_mismatch.jsonl"))
    watch = list(_iter_jsonl(analysis_dir / "watch_screener.jsonl"))

    raw_total = sum(int(row.get("detector_runs") or row.get("raw_setups") or 0) for row in cycles)
    delivered_total = sum(int(row.get("delivered_count") or row.get("delivered_signals") or 0) for row in cycles)
    rejection_counts = _count_rejection_reasons(analysis_dir)
    delivery_status = Counter(str(row.get("delivery_status") or "unknown") for row in delivery)
    funnel_blocks = Counter()
    for row in _iter_jsonl(analysis_dir / "symbol_analysis.jsonl"):
        funnel = row.get("funnel")
        if not isinstance(funnel, dict):
            continue
        for key, value in funnel.items():
            if key.endswith("_blocked") or key.endswith("_rejected"):
                try:
                    funnel_blocks[key] += int(value or 0)
                except (TypeError, ValueError):
                    continue

    prepare_errors = sum(int(row.get("prepare_error_count") or 0) for row in cycles)
    last_cycle = cycles[-1] if cycles else {}

    return {
        "run_id": run_id,
        "analysis_dir": str(analysis_dir),
        "cycles": len(cycles),
        "raw_detector_runs_total": raw_total,
        "candidate_rows": len(candidates),
        "selected_rows": len(selected),
        "delivery_rows": len(delivery),
        "delivered_in_cycles": delivered_total,
        "watch_screener_rows": len(watch),
        "telemetry_mismatch_rows": len(mismatches),
        "prepare_error_count_last_cycle": last_cycle.get("prepare_error_count"),
        "delivery_status_counts": dict(delivery_status),
        "top_rejection_reasons": dict(rejection_counts.most_common(15)),
        "funnel_block_keys": dict(funnel_blocks.most_common(15)),
        "last_cycle": {
            k: last_cycle.get(k)
            for k in (
                "shortlist_size",
                "detector_runs",
                "candidate_count",
                "delivered_count",
                "delivery_success_count",
                "delivery_status_counts",
                "ws_connected",
                "fresh_tickers",
                "fresh_mark_prices",
            )
            if k in last_cycle
        },
    }


def main() -> int:
    bootstrap_repo_path()
    parser = argparse.ArgumentParser(description="Funnel report for latest telemetry run")
    parser.add_argument("--config", default="config.toml")
    parser.add_argument("--run-id", default="", help="Explicit run id under telemetry/runs/")
    parser.add_argument("--json", action="store_true", help="Print JSON only")
    args = parser.parse_args()

    settings = load_settings(config_path=Path(args.config))
    runs_dir = settings.telemetry_dir / "runs"
    run_dir = runs_dir / args.run_id if args.run_id else _latest_run_dir(runs_dir)
    if run_dir is None or not run_dir.exists():
        print("[ERROR] No telemetry run directory found", file=sys.stderr)
        return 1
    analysis_dir = run_dir / "analysis"
    if not analysis_dir.exists():
        print(f"[ERROR] Missing analysis dir: {analysis_dir}", file=sys.stderr)
        return 1

    report = build_report(analysis_dir=analysis_dir, run_id=run_dir.name)
    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return 0

    print(f"[OK] funnel report | run_id={report['run_id']}")
    print(f"  cycles={report['cycles']} raw_runs={report['raw_detector_runs_total']}")
    print(
        f"  candidates={report['candidate_rows']} selected={report['selected_rows']} "
        f"delivery={report['delivery_rows']} mismatches={report['telemetry_mismatch_rows']}"
    )
    print(f"  delivery_status={report['delivery_status_counts']}")
    if report["top_rejection_reasons"]:
        print(f"  top_rejections={report['top_rejection_reasons']}")
    if report["funnel_block_keys"]:
        print(f"  funnel_blocks={report['funnel_block_keys']}")
    print(f"  last_cycle={report['last_cycle']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
