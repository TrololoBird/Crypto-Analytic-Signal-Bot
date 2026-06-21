"""Live soak gate — watch --once + runtime persistence + deep expansion stamp.

Operator-free validation that the Expansion Engine survives a real watch cycle:
  1. Synthetic unit checks (check_expansion)
  2. One watch loop (network, watch_stamp on full/fast tiers)
  3. Runtime state file has FSM + history after shutdown save
  4. Deep assembly stamps row["expansion"] on every pinned anchor
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import Any


def _runtime_snapshot() -> dict[str, Any]:
    from hunt_core.paths import EXPANSION_RUNTIME_STATE_JSON

    if not EXPANSION_RUNTIME_STATE_JSON.is_file():
        return {"fsm_symbols": 0, "history_symbols": 0}
    try:
        raw = json.loads(EXPANSION_RUNTIME_STATE_JSON.read_text(encoding="utf-8"))
        return {
            "fsm_symbols": len(raw.get("fsm") or {}),
            "history_symbols": len(raw.get("history") or {}),
        }
    except (OSError, json.JSONDecodeError):
        return {"fsm_symbols": 0, "history_symbols": 0}


async def _deep_pinned_expansion(
    *,
    min_ok: int,
    symbol_timeout_s: float,
) -> tuple[int, list[dict[str, Any]]]:
    from hunt_core.data.universe import PINNED_SYMBOLS
    from hunt_core.domain.config import load_settings
    from hunt_core.market.factory import create_hunt_market_plane_from_settings
    from hunt_core.runtime.deep_assembly import assemble_deep_tick

    settings = load_settings()
    plane = await create_hunt_market_plane_from_settings(settings)
    client = plane.client
    ok = 0
    rows: list[dict[str, Any]] = []
    for sym in PINNED_SYMBOLS:
        try:
            row = await asyncio.wait_for(
                assemble_deep_tick(sym, client, stagger_ms=120),
                timeout=symbol_timeout_s,
            )
        except asyncio.TimeoutError:
            row = {"symbol": sym, "error": f"deep_timeout_{symbol_timeout_s}s"}
        exp = row.get("expansion") if isinstance(row.get("expansion"), dict) else {}
        sample = {
            "symbol": sym,
            "error": row.get("error"),
            "state": exp.get("state"),
            "expansion_score": exp.get("expansion_score"),
            "trigger_probability": exp.get("trigger_probability"),
            "opportunity_score": (exp.get("meta") or {}).get("opportunity_score"),
        }
        rows.append(sample)
        if row.get("error"):
            print(f"WARN deep {sym}: {row.get('error')}", file=sys.stderr)
        elif exp.get("expansion_score") is None:
            print(f"FAIL deep {sym}: missing expansion stamp", file=sys.stderr)
        else:
            ok += 1
    await plane.close()
    if ok < min_ok:
        print(f"FAIL: expansion stamped on {ok}/{len(PINNED_SYMBOLS)} (need>={min_ok})", file=sys.stderr)
    return ok, rows


async def _run(
    symbols: tuple[str, ...],
    *,
    timeout_s: float,
    skip_unit: bool,
    min_deep_ok: int,
    deep_timeout_s: float,
) -> int:
    fails: list[str] = []

    if not skip_unit:
        from hunt_core._dev import check_expansion

        print("--- unit checks ---")
        if check_expansion.main() != 0:
            fails.append("unit checks")
        else:
            print("unit: OK")

    before = _runtime_snapshot()
    print("runtime before:", json.dumps(before))

    from hunt_core._dev.watch_once_smoke import _kill_legacy_watchers, _run_once

    _kill_legacy_watchers()
    print(f"\n--- watch --once {symbols} ---")
    if await _run_once(symbols, timeout_s=timeout_s) != 0:
        fails.append("watch --once")

    after = _runtime_snapshot()
    print("runtime after:", json.dumps(after))
    if after["fsm_symbols"] < 1:
        fails.append("runtime FSM empty after watch")
    if after["history_symbols"] < 1:
        fails.append("runtime history empty after watch")

    print("\n--- deep pinned expansion ---")
    ok, rows = await _deep_pinned_expansion(min_ok=min_deep_ok, symbol_timeout_s=deep_timeout_s)
    print(json.dumps(rows, ensure_ascii=False, indent=2))

    if ok < min_deep_ok:
        fails.append(f"deep expansion {ok} < min {min_deep_ok}")

    if fails:
        for f in fails:
            print(f"FAIL: {f}", file=sys.stderr)
        return 1
    print("\nOK: expansion watch soak")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Expansion Engine live soak gate")
    parser.add_argument("symbols", nargs="*", default=["BTCUSDT", "SOLUSDT"])
    parser.add_argument("--timeout", type=float, default=180.0)
    parser.add_argument("--skip-unit", action="store_true")
    parser.add_argument(
        "--min-deep-ok",
        type=int,
        default=2,
        help="Minimum pinned symbols with expansion stamp (market errors are WARN)",
    )
    parser.add_argument(
        "--deep-timeout",
        type=float,
        default=90.0,
        help="Per-symbol timeout for deep assembly probe",
    )
    args = parser.parse_args(argv)
    symbols = tuple(dict.fromkeys(s.upper() for s in args.symbols))
    return asyncio.run(
        _run(
            symbols,
            timeout_s=args.timeout,
            skip_unit=args.skip_unit,
            min_deep_ok=args.min_deep_ok,
            deep_timeout_s=args.deep_timeout,
        ),
    )


if __name__ == "__main__":
    raise SystemExit(main())
