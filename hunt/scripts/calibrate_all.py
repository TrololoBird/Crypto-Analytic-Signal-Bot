#!/usr/bin/env python3
"""Full hunt calibration: universal gates + per-symbol overrides (REST + outcomes)."""

from __future__ import annotations

import argparse
import json
import sys

from hunt_watch.bootstrap import bootstrap

bootstrap()

from hunt_watch.calibration import compute_auto_calibration
from hunt_core.calibrate.runner import run_full_calibration
from hunt_watch.paths import SIGNAL_STATE


def _print_auto_calibration() -> None:
    """Print auto-calibration suggestions from closed_history."""
    try:
        state = json.loads(SIGNAL_STATE.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"[auto-cal] cannot read signal state: {exc}", file=sys.stderr)
        return
    result = compute_auto_calibration(state)
    print("\n=== AUTO-CALIBRATION (closed_history) ===")
    for line in result["suggestions"]:
        print(f"  {line}")
    if result["adjustments"]:
        print(f"\n  adjustments: {json.dumps(result['adjustments'], indent=4, default=str)}")
    print(f"\n  safe_to_apply={result['safe_to_apply']}  (read-only — apply manually if confirmed)")
    print("=" * 45)


def main() -> int:
    parser = argparse.ArgumentParser(description="Calibrate hunt universal + per-symbol parameters")
    parser.add_argument("--no-rest", action="store_true", help="Skip Binance REST volatility profiles")
    parser.add_argument("--no-backfill", action="store_true", help="Skip legacy outcome REST backfill")
    parser.add_argument("--rest-limit", type=int, default=40, help="Max symbols for REST 7d profile")
    parser.add_argument("--no-auto-cal", action="store_true", help="Skip auto-calibration from closed_history")
    args = parser.parse_args()

    if not args.no_auto_cal:
        _print_auto_calibration()

    payload = run_full_calibration(
        fetch_rest=not args.no_rest,
        backfill=not args.no_backfill,
        rest_symbol_limit=max(5, args.rest_limit),
    )
    print(json.dumps(payload, indent=2, default=str))
    conf = (payload.get("data_summary") or {}).get("confidence")
    n_lab = (payload.get("data_summary") or {}).get("n_labeled")
    print(
        f"\nSaved → hunt/data/adaptive_thresholds.json | "
        f"labeled={n_lab} per_symbol={len(payload.get('per_symbol') or {})} confidence={conf}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
