#!/usr/bin/env python3
"""Independent multi-symbol hunt analysis — raw REST, no hunt heuristics."""

from __future__ import annotations

import asyncio
import importlib.util
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from hunt_watch.bootstrap import bootstrap

bootstrap()


def _load_analyze():
    path = Path(__file__).resolve().parent / "beat_check.py"
    spec = importlib.util.spec_from_file_location("hunt_beat_check", path)
    if spec is None or spec.loader is None:
        msg = f"cannot load {path}"
        raise ImportError(msg)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.analyze


from hunt_watch.paths import SNAPSHOTS

_analyze_one = _load_analyze()

CORE = ("BTCUSDT", "ETHUSDT", "XAUUSDT", "XAGUSDT", "PLAYUSDT", "HOMEUSDT")


async def batch(symbols: tuple[str, ...]) -> dict[str, Any]:
    results: dict[str, Any] = {}
    for sym in symbols:
        try:
            results[sym] = await _analyze_one(sym)
        except Exception as exc:  # noqa: BLE001
            results[sym] = {"symbol": sym, "error": repr(exc)}
    return {"ts": datetime.now(UTC).isoformat(), "symbols": results}


def main() -> None:
    import sys

    syms = tuple(s.upper() for s in (sys.argv[1:] or CORE))
    out = asyncio.run(batch(syms))
    path = SNAPSHOTS / "hunt_independent_batch.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(out, indent=2), encoding="utf-8")
    for sym, r in out["symbols"].items():
        if r.get("error"):
            print(sym, "ERR", r["error"])
            continue
        ind = r.get("independent", {})
        lv = r.get("levels", {})
        print(
            f"{sym} ${r['price']} 24h={r['change_24h_pct']}% pos={r['pos_in_range_24h']} "
            f"| {ind.get('bias')} short={ind.get('score_short')} long={ind.get('score_long')}"
        )
        print(
            f"  bounce={lv.get('bounce_from_1h_low_pct')}% taker5m={r.get('taker_5m')} fund={r.get('funding_pct')}"
        )
        print(f"  short: {ind.get('reasons_short')}")
        print(f"  long:  {ind.get('reasons_long')}")


if __name__ == "__main__":
    main()
