#!/usr/bin/env python3
"""BEAT dump experiment — 60s REST loop, full indicator matrix, ranked scenarios."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from hunt_watch.bootstrap import bootstrap

bootstrap()

from hunt_watch.beat_dump_lab import TickState, make_client, run_tick
from hunt_watch.paths import DATA
from hunt_watch.scriptutil import configure_script_logging

OUT_DIR = DATA / "experiments" / "beat_dump"
JSONL = OUT_DIR / "beat_dump_ticks.jsonl"
SUMMARY_LOG = OUT_DIR / "beat_dump.log"


def _format_summary(row: dict) -> str:
    if row.get("verdict") == "DATA_INCOMPLETE" or row.get("analyzable") is False:
        sym = row.get("symbol", "?")
        n = row.get("violation_count", 0)
        lines = [f"!!! DATA_INCOMPLETE {sym} — {n} violation(s) — NO SIGNAL OUTPUT !!!"]
        for v in (row.get("violations") or [])[:12]:
            lines.append(f"  · {v}")
        if n > 12:
            lines.append(f"  · ... +{n - 12} more")
        return "\n".join(lines)

    sym = row.get("symbol", "?")
    price = row.get("price")
    verdict = row.get("verdict", "?")
    composite = row.get("composite_dump_score")
    align = row.get("cross_tf_alignment")
    clusters = row.get("cluster_scores") or {}
    lifecycle = (row.get("lifecycle") or {}).get("phase", "?")
    n_feat = row.get("total_feature_points", 0)
    n_cols = row.get("indicator_columns_per_tf", 0)
    lines = [
        f"=== {sym} @ {price} | {verdict} | score={composite} align={align} | {n_cols} cols × 12 panels = {n_feat} pts ===",
        "clusters: "
        + " · ".join(f"{k}={v:.2f}" for k, v in sorted(clusters.items(), key=lambda x: -x[1])),
        f"lifecycle: {lifecycle} · fall={(row.get('lifecycle') or {}).get('fall_from_high_pct')}%",
    ]
    mkt = row.get("market_layer") or {}
    if mkt.get("signals"):
        lines.append(
            "market: "
            + " | ".join(f"{s['id']}({s['score']:.2f})" for s in mkt["signals"][:5])
        )
    delta = row.get("delta_60s") or {}
    if delta.get("warming"):
        lines.append(f"Δ60s warming: {', '.join(delta['warming'])}")
    if delta.get("cooling"):
        lines.append(f"Δ60s cooling: {', '.join(delta['cooling'])}")
    scenarios = row.get("scenarios") or []
    if scenarios:
        lines.append("scenarios:")
        for s in scenarios[:4]:
            lines.append(
                f"  [{s['confidence']:>3}] {s['name']} ({s['horizon']}) — met={len(s.get('triggers_met') or [])} miss={len(s.get('triggers_missing') or [])}"
            )
        top = (scenarios[0].get("evidence") or [])[:5]
        if top:
            lines.append(
                "  top evidence: "
                + ", ".join(f"{e['tf']}:{e['indicator']}({e['contrib']:.2f})" for e in top)
            )
    else:
        lines.append("scenarios: none above threshold")
    ev = row.get("top_evidence") or []
    if ev:
        lines.append(
            "global evidence: "
            + ", ".join(f"{e['tf']}:{e['indicator']}" for e in ev[:8])
        )
    return "\n".join(lines)


async def _loop(symbol: str, interval: int, *, once: bool) -> int:
    log = configure_script_logging("beat_dump_experiment")
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    client = make_client()
    prior: TickState | None = None
    try:
        while True:
            try:
                row, prior = await run_tick(client, symbol, prior=prior)
            except Exception as exc:
                log.error("tick_exception", error=str(exc), exc_type=type(exc).__name__)
                if once:
                    return 1
                await asyncio.sleep(interval)
                continue
            if row.get("verdict") == "DATA_INCOMPLETE":
                log.error(
                    "data_incomplete",
                    symbol=row.get("symbol"),
                    violations=row.get("violation_count"),
                )
            if row.get("error"):
                log.error("tick_failed", error=row["error"])
                if once:
                    return 1
                await asyncio.sleep(interval)
                continue
            with JSONL.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
            summary = _format_summary(row)
            print(summary, flush=True)
            with SUMMARY_LOG.open("a", encoding="utf-8") as fh:
                fh.write(f"\n--- {datetime.now(UTC).isoformat()} ---\n{summary}\n")
            if once:
                return 0
            await asyncio.sleep(interval)
    finally:
        await client.close()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="BEAT dump experiment (full indicator matrix)")
    p.add_argument("--symbol", default="BEATUSDT")
    p.add_argument("--interval", type=int, default=60, help="seconds between REST polls")
    p.add_argument("--once", action="store_true", help="single tick then exit")
    args = p.parse_args(argv)
    return asyncio.run(_loop(args.symbol.upper(), max(10, args.interval), once=args.once))


if __name__ == "__main__":
    sys.exit(main())
