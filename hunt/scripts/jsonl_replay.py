#!/usr/bin/env python3
"""Offline JSONL replay — confirm_min sweep + walk-forward + prep funnel."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from hunt_watch.bootstrap import bootstrap

bootstrap()

from hunt_watch.jsonl_replay import run_replay_report
from hunt_watch.paths import DATA, SESSION_DIR
from hunt_watch.scriptutil import configure_script_logging

LOG = configure_script_logging("hunt.jsonl_replay")
DEFAULT_OUT = SESSION_DIR / "jsonl_replay_report.json"


def main() -> int:
    parser = argparse.ArgumentParser(description="Hunt JSONL replay MVP")
    parser.add_argument("--max-lines", type=int, default=8000, help="Tail lines to scan")
    parser.add_argument("--symbols", nargs="*", help="Filter symbols e.g. BEATUSDT")
    parser.add_argument(
        "--floors",
        nargs="*",
        type=int,
        default=list(range(60, 73, 2)),
        help="confirm_min floors to sweep",
    )
    parser.add_argument("--no-walk-forward", action="store_true", help="Skip IS/OOS split")
    parser.add_argument("--out", type=str, default="", help="Report path (default session/)")
    args = parser.parse_args()

    sym_set = {s.upper() for s in args.symbols} if args.symbols else None
    floors = tuple(sorted(set(args.floors)))
    report = run_replay_report(
        max_lines=args.max_lines,
        symbols=sym_set,
        floors=floors,
        walk_forward=not args.no_walk_forward,
    )
    text = json.dumps(report, indent=2, default=str)
    out_path = Path(args.out) if args.out else DEFAULT_OUT
    if not out_path.is_absolute():
        out_path = DATA / out_path
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(text, encoding="utf-8")
    print(f"Wrote {out_path}", file=sys.stderr)

    if "error" in report:
        print(text)
        return 1

    sweep = report.get("sweep") or {}
    wf = report.get("walk_forward") or {}
    prep = report.get("prep_funnel") or {}
    pshadow = report.get("prep_shadow_replay") or {}
    print(
        f"replay n={report.get('n_rows')} dropped={report.get('rows_dropped_no_closed_bars')} "
        f"floor={sweep.get('recommended_floor')} wf_pick={wf.get('picked_confirm_min')}",
        file=sys.stderr,
    )
    if prep.get("prep_sends"):
        print(
            f"prep funnel: {prep.get('conversion_pct')}% within {prep.get('window_hours')}h "
            f"({prep.get('confirmed_within_window')}/{prep.get('prep_sends')})",
            file=sys.stderr,
        )
    if pshadow.get("n"):
        print(
            f"prep shadow WR: {pshadow.get('direction_wr_pct')}% "
            f"tiers={pshadow.get('by_tier')}",
            file=sys.stderr,
        )
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
