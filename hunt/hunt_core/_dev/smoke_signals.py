"""Live /signals smoke — baseline snapshot for phase regression (§R.3)."""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import Any

DEFAULT_SYMBOLS = (
    "BTCUSDT",
    "ETHUSDT",
    "XAUUSDT",
    "XAGUSDT",
    "PEPEUSDT",
    "WIFUSDT",
    "DOGEUSDT",
)


def _compact_row(row: dict[str, Any]) -> dict[str, Any]:
    from hunt_core.gate._ev import setup_conviction_pct

    lc = row.get("lifecycle") or {}
    dump = row.get("dump") or {}
    long = row.get("long") or {}
    short_c = setup_conviction_pct(dump, direction="short")
    long_c = setup_conviction_pct(long, direction="long")
    setup_s = dump if dump.get("confirmed") or short_c >= long_c else long
    levels = {
        k: setup_s.get(k)
        for k in ("entry_zone", "stop_loss", "tp1", "tp2", "risk_reward", "levels_viable")
        if setup_s.get(k) is not None
    }
    out: dict[str, Any] = {
        "symbol": row.get("symbol"),
        "error": row.get("error"),
        "price": row.get("price"),
        "phase": lc.get("phase"),
        "bias": lc.get("recommended_bias"),
        "short_conf": dump.get("confirmed"),
        "long_conf": long.get("confirmed"),
        "short_conviction": round(short_c, 1),
        "long_conviction": round(long_c, 1),
    }
    if levels:
        out["levels"] = levels
    pinned = row.get("pinned_scenario") or row.get("pinned_verdict")
    if pinned:
        out["pinned"] = pinned
    return out


async def _probe_symbols(symbols: tuple[str, ...]) -> list[dict[str, Any]]:
    from hunt_core.bootstrap import bootstrap

    bootstrap()
    from hunt_core.runtime.symbol_probe import probe_symbol_signal

    rows: list[dict[str, Any]] = []
    for sym in symbols:
        try:
            row = await probe_symbol_signal(sym, auto_watchlist=False, stagger_ms=200)
        except Exception as exc:  # noqa: BLE001
            row = {"symbol": sym, "error": f"{type(exc).__name__}: {exc}"}
        rows.append(_compact_row(row))
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Hunt /signals smoke baseline")
    parser.add_argument("symbols", nargs="*", default=list(DEFAULT_SYMBOLS))
    parser.add_argument("--dump", action="store_true", help="Print JSON to stdout")
    parser.add_argument("--baseline", type=str, default="", help="Compare against baseline JSON file")
    args = parser.parse_args(argv)
    symbols = tuple(str(s).upper() for s in args.symbols)
    rows = asyncio.run(_probe_symbols(symbols))
    payload = {"symbols": list(symbols), "rows": rows}
    if args.baseline:
        baseline_path = args.baseline
        try:
            baseline = json.loads(open(baseline_path, encoding="utf-8").read())
        except OSError as exc:
            print(f"FAIL: cannot read baseline {baseline_path}: {exc}", file=sys.stderr)
            return 1
        base_rows = {r.get("symbol"): r for r in baseline.get("rows") or []}
        regressions: list[str] = []
        for row in rows:
            sym = row.get("symbol")
            old = base_rows.get(sym)
            if old is None:
                regressions.append(f"{sym}: new symbol (no baseline)")
                continue
            if row.get("error") and not old.get("error"):
                regressions.append(f"{sym}: new error {row.get('error')}")
        if regressions:
            for line in regressions:
                print(line, file=sys.stderr)
            return 1
        print(f"OK: {len(rows)} symbols vs baseline")
        return 0
    text = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
    if args.dump:
        print(text)
    else:
        for row in rows:
            err = row.get("error")
            tag = f" ERR={err}" if err else ""
            print(
                f"{row.get('symbol')} phase={row.get('phase')} bias={row.get('bias')}{tag}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
