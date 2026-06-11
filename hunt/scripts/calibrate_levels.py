#!/usr/bin/env python3
"""Calibrate adaptive level params from hunt tracker outcomes + probe sample."""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from hunt_watch.bootstrap import bootstrap

bootstrap()

from hunt_watch.level_calibration import calibrate_from_outcomes
from hunt_watch.param_store import save_calibration_payload
from hunt_watch.paths import SIGNAL_STATE
from hunt_watch.symbol_probe import probe_symbol_signal


async def _probe_levels(symbols: tuple[str, ...]) -> list[dict]:
    rows: list[dict] = []
    for sym in symbols:
        try:
            row = await probe_symbol_signal(sym, auto_watchlist=False, stagger_ms=80)
        except Exception as exc:  # noqa: BLE001
            rows.append({"symbol": sym, "error": repr(exc)})
            continue
        dump = row.get("dump") or {}
        long_s = row.get("long") or {}
        rows.append(
            {
                "symbol": sym,
                "range_24h": (row.get("session") or {}).get("range_pct_24h"),
                "dump_viable": dump.get("levels_viable"),
                "dump_veto": dump.get("levels_veto"),
                "dump_sl_pct": dump.get("sl_dist_pct"),
                "long_viable": long_s.get("levels_viable"),
                "long_veto": long_s.get("levels_veto"),
            }
        )
    return rows


def main() -> int:
    state = json.loads(SIGNAL_STATE.read_text(encoding="utf-8"))
    closed = [
        v for v in (state.get("signals") or {}).values()
        if isinstance(v, dict) and v.get("status") == "closed"
    ]
    cal = calibrate_from_outcomes(closed)
    syms = tuple(s for s in (sys.argv[1:] or ("VELVETUSDT", "BEATUSDT", "HMSTRUSDT")))
    probes = asyncio.run(_probe_levels(syms))
    out = {
        "computed_at": datetime.now(UTC).isoformat(),
        "outcome_calibration": cal,
        "probe_sample": probes,
    }
    save_calibration_payload(out)
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
