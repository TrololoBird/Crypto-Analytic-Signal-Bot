"""Live smoke — Expansion Engine on BTC + alt probe (plan verification)."""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import Any

_EXPANSION_KEYS = (
    "state",
    "dominant",
    "expansion_score",
    "trigger_probability",
    "lifecycle_stage",
)
_META_KEYS = ("expansion_quality", "opportunity_score", "fake_breakout_risk")
_PROB_KEYS = ("p_up", "p_down", "p_none")


def _check_row(row: dict[str, Any]) -> list[str]:
    fails: list[str] = []
    sym = row.get("symbol") or "?"
    if row.get("error"):
        fails.append(f"{sym}: probe error {row['error']}")
        return fails
    exp = row.get("expansion")
    if not isinstance(exp, dict):
        fails.append(f"{sym}: missing row['expansion']")
        return fails
    for key in _EXPANSION_KEYS:
        if exp.get(key) is None:
            fails.append(f"{sym}: expansion missing {key}")
    meta = exp.get("meta")
    if not isinstance(meta, dict):
        fails.append(f"{sym}: expansion missing meta")
    else:
        for key in _META_KEYS:
            if meta.get(key) is None:
                fails.append(f"{sym}: meta missing {key}")
    probs = exp.get("probabilities")
    if not isinstance(probs, dict):
        fails.append(f"{sym}: expansion missing probabilities")
    else:
        for key in _PROB_KEYS:
            if probs.get(key) is None:
                fails.append(f"{sym}: probabilities missing {key}")
    blocks = exp.get("blocks")
    if not isinstance(blocks, dict) or len(blocks) < 8:
        fails.append(f"{sym}: too few block scores ({len(blocks) if isinstance(blocks, dict) else 0})")
    price = float(row.get("price") or 0)
    if price <= 0:
        fails.append(f"{sym}: invalid price")
    return fails


async def _run(symbols: tuple[str, ...]) -> tuple[list[dict[str, Any]], list[str]]:
    from hunt_core.bootstrap import bootstrap

    bootstrap()
    from hunt_core.runtime.expansion_probe import probe_symbol_expansion

    rows: list[dict[str, Any]] = []
    fails: list[str] = []
    for sym in symbols:
        row = await probe_symbol_expansion(sym, stagger_ms=300)
        rows.append(row)
        fails.extend(_check_row(row))
    return rows, fails


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Expansion Engine live smoke")
    parser.add_argument("symbols", nargs="*", default=["BTCUSDT", "SOLUSDT"])
    parser.add_argument("--dump", action="store_true")
    args = parser.parse_args(argv)
    symbols = tuple(dict.fromkeys(s.upper() for s in args.symbols))
    rows, fails = asyncio.run(_run(symbols))
    if args.dump:
        compact = []
        for row in rows:
            exp = row.get("expansion") or {}
            compact.append(
                {
                    "symbol": row.get("symbol"),
                    "price": row.get("price"),
                    "state": exp.get("state"),
                    "dominant": exp.get("dominant"),
                    "expansion_score": exp.get("expansion_score"),
                    "trigger_probability": exp.get("trigger_probability"),
                    "meta": exp.get("meta"),
                    "probabilities": exp.get("probabilities"),
                }
            )
        print(json.dumps(compact, ensure_ascii=False, indent=2, default=str))
    else:
        for row in rows:
            exp = row.get("expansion") or {}
            print(
                f"{row.get('symbol')} state={exp.get('state')} "
                f"dom={exp.get('dominant')} score={exp.get('expansion_score')} "
                f"trig={exp.get('trigger_probability')}"
            )
    if fails:
        for f in fails:
            print(f"FAIL: {f}", file=sys.stderr)
        return 1
    print(f"OK: expansion probe on {len(rows)} symbols")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
