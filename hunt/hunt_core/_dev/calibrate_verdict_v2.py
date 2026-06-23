"""Calibrate Verdict V2 gates from deep_ticks.jsonl or live probe."""
from __future__ import annotations

import argparse
import asyncio
import json
import sys

from hunt_core.deep.verdict_v2.calibration import (
    CALIBRATION_JSON,
    aggregate_calibration,
    load_deep_tick_summaries,
    suggest_gates,
    write_gate_overrides,
)
from hunt_core.data.universe import PINNED_SYMBOLS
from hunt_core.domain.config import load_settings
from hunt_core.market.factory import create_hunt_market_plane_from_settings
from hunt_core.runtime.deep_assembly import assemble_deep_tick


def _print_report(report: dict[str, object]) -> None:
    print(json.dumps(report, indent=2, ensure_ascii=False))
    blockers = report.get("top_blockers") or []
    rate = report.get("signal_rate")
    print(f"\nSignal rate: {rate} · top blockers: {', '.join(blockers) if blockers else '—'}", file=sys.stderr)
    sg = report.get("suggested_gates")
    if isinstance(sg, dict) and sg.get("applied"):
        print(
            f"Suggested strength_min={sg.get('strength_min')} "
            f"(sim rate {sg.get('simulated_rate')}, p30={sg.get('strength_p30')})",
            file=sys.stderr,
        )
    per = report.get("per_symbol") or {}
    for sym, stats in sorted(per.items()):
        if not isinstance(stats, dict):
            continue
        print(
            f"  {sym}: n={stats.get('samples')} signal_rate={stats.get('signal_rate')} "
            f"avg_strength={stats.get('avg_strength')} gates={stats.get('gate_failures')}",
            file=sys.stderr,
        )


async def _live_samples() -> list[dict]:
    settings = load_settings()
    plane = await create_hunt_market_plane_from_settings(settings)
    client = plane.client
    out: list[dict] = []
    for sym in PINNED_SYMBOLS:
        row = await assemble_deep_tick(sym, client, stagger_ms=80)
        if row.get("error"):
            continue
        summary = row.get("verdict_v2_summary")
        if isinstance(summary, dict):
            out.append({"ts": row.get("ts"), "symbol": sym, **summary})
    await plane.close()
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="Verdict V2 gate calibration rollup")
    parser.add_argument("--live", action="store_true", help="Use live deep ticks only (no JSONL)")
    parser.add_argument("--limit", type=int, default=500, help="Max JSONL rows to scan")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write data/verdict_v2_gate_overrides.json from suggested gates",
    )
    args = parser.parse_args()

    if args.live:
        summaries = asyncio.run(_live_samples())
    else:
        summaries = load_deep_tick_summaries(limit=args.limit)
        if not summaries:
            print("No verdict_v2_summary in deep_ticks.jsonl — running live probe…", file=sys.stderr)
            summaries = asyncio.run(_live_samples())

    report = aggregate_calibration(summaries)
    sg = suggest_gates(summaries, min_samples=4 if args.live else 12)
    report["suggested_gates"] = sg

    CALIBRATION_JSON.parent.mkdir(parents=True, exist_ok=True)
    CALIBRATION_JSON.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Wrote {CALIBRATION_JSON}", file=sys.stderr)

    if args.apply and sg.get("applied"):
        path = write_gate_overrides(sg)
        print(f"Applied gate overrides → {path}", file=sys.stderr)

    _print_report(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
