"""Probe verdict_v2 gate breakdown for pinned symbols."""
from __future__ import annotations

import asyncio
import json

from hunt_core.data.universe import PINNED_SYMBOLS
from hunt_core.domain.config import load_settings
from hunt_core.market.factory import create_hunt_market_plane_from_settings
from hunt_core.runtime.deep_assembly import assemble_deep_tick


async def _main() -> int:
    settings = load_settings()
    plane = await create_hunt_market_plane_from_settings(settings)
    client = plane.client
    for sym in PINNED_SYMBOLS:
        row = await assemble_deep_tick(sym, client, stagger_ms=80)
        if row.get("error"):
            print(json.dumps({"symbol": sym, "error": row["error"]}))
            continue
        summary = row.get("verdict_v2_summary") or {}
        v2 = row.get("verdict_v2")
        gates = list(getattr(getattr(v2, "signal_decision", None), "gates_failed", []) or summary.get("gates_failed") or [])
        print(
            json.dumps(
                {
                    "symbol": sym,
                    "action": summary.get("action"),
                    "path": summary.get("path"),
                    "strength": summary.get("strength"),
                    "fragility": summary.get("fragility"),
                    "rr_primary": summary.get("rr_primary"),
                    "data_coverage": summary.get("data_coverage"),
                    "gates_failed": gates,
                    "horizon_b": summary.get("horizon_b_conviction"),
                    "range_p": summary.get("range_probability"),
                },
                ensure_ascii=False,
            )
        )
    await plane.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
