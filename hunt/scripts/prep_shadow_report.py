#!/usr/bin/env python3
"""Report prep/imminent/start shadow outcomes — direction WR + paper PnL."""

from __future__ import annotations

import argparse
import json
import sys

from hunt_watch.bootstrap import bootstrap

bootstrap()

from hunt_watch.paths import SESSION_DIR
from hunt_watch.prep_shadow_tracker import (
    format_prep_shadow_html,
    format_prep_shadow_text,
    load_prep_shadow_state,
    summarize_prep_shadows,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Prep shadow calibration report")
    parser.add_argument("--json", action="store_true", help="JSON output")
    parser.add_argument("--html", action="store_true", help="Telegram HTML")
    parser.add_argument("--out", type=str, default="", help="Write report path")
    args = parser.parse_args()

    state = load_prep_shadow_state()
    summary = summarize_prep_shadows(state)
    if args.json:
        payload = {
            "n_closed": summary.n_closed,
            "n_active": summary.n_active,
            "direction_wr": summary.direction_wr,
            "avg_mfe": summary.avg_mfe,
            "avg_paper_pnl": summary.avg_paper_pnl,
            "confirm_rate": summary.confirm_rate,
            "by_tier": summary.by_tier,
            "by_phase": summary.by_phase,
        }
        text = json.dumps(payload, indent=2)
    elif args.html:
        text = format_prep_shadow_html(summary)
    else:
        text = format_prep_shadow_text(summary)

    if args.out:
        out = SESSION_DIR / args.out if not args.out.startswith("/") else args.out
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
        print(f"Wrote {out}", file=sys.stderr)
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
