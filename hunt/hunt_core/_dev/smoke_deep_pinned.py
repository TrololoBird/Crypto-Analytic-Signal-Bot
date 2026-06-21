"""Smoke: deep assembly for all pinned anchors (Module 2, no Telegram)."""
from __future__ import annotations

import asyncio
import json
import sys

from hunt_core.data.universe import PINNED_SYMBOLS
from hunt_core.domain.config import load_settings
from hunt_core.market.factory import create_hunt_market_plane_from_settings
from hunt_core.runtime.deep_assembly import assemble_deep_tick


async def _main() -> int:
    settings = load_settings()
    plane = await create_hunt_market_plane_from_settings(settings)
    client = plane.client
    ok = 0
    for sym in PINNED_SYMBOLS:
        row = await assemble_deep_tick(sym, client, stagger_ms=100)
        if row.get("error"):
            print(f"FAIL {sym}: {row['error']}", file=sys.stderr)
            continue
        if row.get("plane") != "deep":
            print(f"FAIL {sym}: plane={row.get('plane')}", file=sys.stderr)
            continue
        if not row.get("pinned_verdict"):
            print(f"FAIL {sym}: missing pinned_verdict", file=sys.stderr)
            continue
        v2 = row.get("verdict_v2")
        if v2 is None:
            print(f"FAIL {sym}: missing verdict_v2", file=sys.stderr)
            continue
        if not row.get("signal_queue"):
            print(f"WARN {sym}: missing signal_queue", file=sys.stderr)
        exp = row.get("expansion") if isinstance(row.get("expansion"), dict) else {}
        if exp.get("expansion_score") is None:
            print(f"FAIL {sym}: missing expansion stamp", file=sys.stderr)
            continue
        ok += 1
        pv = row["pinned_verdict"]
        dec = v2.signal_decision
        path = v2.expected_path
        plan = v2.trade_plan
        print(
            json.dumps(
                {
                    "symbol": sym,
                    "plane": row.get("plane"),
                    "verdict": getattr(pv, "kind", None),
                    "action": dec.action,
                    "path": path.type,
                    "strength": v2.signal_strength.score,
                    "has_plan": plan is not None,
                    "confidence": getattr(pv, "confidence", None),
                    "expansion_state": exp.get("state"),
                    "expansion_score": exp.get("expansion_score"),
                    "trigger_probability": exp.get("trigger_probability"),
                }
            )
        )
    await plane.close()
    return 0 if ok == len(PINNED_SYMBOLS) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
