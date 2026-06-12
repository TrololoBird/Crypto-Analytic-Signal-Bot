#!/usr/bin/env python3
"""Build intel dossier for Claude Code / Cursor; optional Gemini autonomous path.

Never writes hunt_calibration.json or touches the hot path.

Usage:
    PYTHONPATH=hunt python hunt/scripts/analyze_session.py
    PYTHONPATH=hunt python hunt/scripts/analyze_session.py --gemini
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from hunt_watch.bootstrap import bootstrap

bootstrap()


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Build hunt intel dossier (offline analyst)")
    p.add_argument(
        "--gemini",
        action="store_true",
        help="if GEMINI_API_KEY set, auto-call Gemini and write intel_report.json",
    )
    return p.parse_args()


async def _main_async(args: argparse.Namespace) -> int:
    from intel.dossier import build_intel_dossier, write_intel_dossier
    from intel.provider import analyze_with_gemini, gemini_available
    from intel.report import save_intel_report
    from hunt_watch.paths import HUNT_CALIBRATION, INTEL_DOSSIER_MD, INTEL_REPORT

    dossier = build_intel_dossier()
    md_path, json_path = write_intel_dossier(dossier)
    n = int(dossier.get("n_live_closed") or 0)

    print(f"Wrote {md_path}")
    print(f"Wrote {json_path}")

    if args.gemini and gemini_available():
        md_text = md_path.read_text(encoding="utf-8")
        report = await analyze_with_gemini(md_text, n_signals=n)
        if report:
            ok, errors = save_intel_report(report)
            if ok:
                print(f"Wrote {INTEL_REPORT} (Gemini)")
            else:
                print(f"Gemini report invalid: {errors}", file=sys.stderr)
        else:
            print("Gemini call failed — dossier still available.", file=sys.stderr)
    else:
        print("")
        print("── Next step (primary path, zero API cost) ──")
        print(f"Open `{INTEL_REPORT.parent / 'intel_dossier.md'}` in Claude Code / Cursor.")
        print("Ask the agent to return suggestions as JSON → save as hunt/data/intel_report.json")
        print("")
        print(f"Guardrail: this script never writes {HUNT_CALIBRATION.name}")

    return 0


def main() -> int:
    args = _parse_args()
    return asyncio.run(_main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
