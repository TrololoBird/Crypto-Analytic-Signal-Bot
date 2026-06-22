"""Calibrate Expansion Engine block weights from the outcome ledger."""
from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime

from hunt_core._dev.expansion_lab.config import invalidate_expansion_config_cache
from hunt_core._dev.expansion_lab.format import (
    format_calibration_report,
    format_outcome_stats,
)
from hunt_core._dev.expansion_lab.learning import (
    load_expansion_outcomes,
    summarize_outcomes,
    write_calibration_rollup,
)
from hunt_core._dev.expansion_lab.learning.calibration import calibrate_block_weights
from hunt_core._dev.expansion_lab.learning.review import pending_review_horizons
from hunt_core.paths import EXPANSION_CALIBRATION_JSON


def _persist_report(report: dict[str, object]) -> None:
    payload = dict(report)
    payload["computed_at"] = datetime.now(UTC).isoformat()
    EXPANSION_CALIBRATION_JSON.parent.mkdir(parents=True, exist_ok=True)
    EXPANSION_CALIBRATION_JSON.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    invalidate_expansion_config_cache()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Expansion Engine block-weight calibration")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Write calibration file even when samples < 20",
    )
    parser.add_argument("--json", action="store_true", help="Print raw JSON report only")
    args = parser.parse_args(argv)

    records = load_expansion_outcomes()
    summary = summarize_outcomes(records)
    pending = sum(1 for rec in records if pending_review_horizons(rec))

    if args.force:
        report = calibrate_block_weights(records)
        _persist_report(report)
    else:
        report = write_calibration_rollup(records)

    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return 0

    print(format_outcome_stats(summary, pending_reviews=pending, records=len(records)))
    print()
    print(format_calibration_report(report))
    if report.get("status") == "ok" or args.force:
        print(f"\nWrote {EXPANSION_CALIBRATION_JSON}", file=sys.stderr)
    else:
        print("\nInsufficient samples for calibration", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
