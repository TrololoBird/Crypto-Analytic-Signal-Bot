#!/usr/bin/env python3
"""Bot vs independent Binance REST diff — no web search, API only."""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from hunt_watch.bootstrap import bootstrap

bootstrap()

from hunt_watch.paths import TICK_JSONL
from hunt_watch.targets import DEFAULT_SYMBOLS
from hunt_watch.verify_diff import (
    compare_row,
    format_diff_table,
    load_latest_bot_ticks,
    load_watchlist_symbols,
    save_diff_report,
)


def _load_analyze():
    path = Path(__file__).resolve().parent / "beat_check.py"
    spec = importlib.util.spec_from_file_location("hunt_beat_check", path)
    if spec is None or spec.loader is None:
        msg = f"cannot load {path}"
        raise ImportError(msg)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.analyze


async def run_diff(symbols: tuple[str, ...]) -> list:
    analyze = _load_analyze()
    bot_ticks = load_latest_bot_ticks(symbols=set(symbols))
    rows = []
    for sym in symbols:
        try:
            ind = await analyze(sym)
        except Exception as exc:  # noqa: BLE001
            ind = {"symbol": sym, "error": repr(exc)}
        rows.append(compare_row(bot_ticks.get(sym.upper()), ind))
    return rows


def _resolve_symbols(args: argparse.Namespace) -> tuple[str, ...]:
    if args.symbols:
        return tuple(dict.fromkeys(s.upper() for s in args.symbols))
    if args.watchlist:
        wl = load_watchlist_symbols()
        if wl:
            return tuple(dict.fromkeys(wl[: args.limit]))
    core = list(DEFAULT_SYMBOLS)
    wl = load_watchlist_symbols()[: max(0, args.limit - len(core))]
    return tuple(dict.fromkeys([*core, *wl]))


def main() -> None:
    parser = argparse.ArgumentParser(description="Hunt bot tick vs independent Binance REST")
    parser.add_argument("symbols", nargs="*", help="Symbols (default: pinned + watchlist)")
    parser.add_argument("--watchlist", action="store_true", help="Use watchlist only")
    parser.add_argument("--limit", type=int, default=15, help="Max symbols from watchlist")
    parser.add_argument("--json", action="store_true", help="Print full JSON report")
    args = parser.parse_args()
    symbols = _resolve_symbols(args)
    if not symbols:
        print("no symbols", file=sys.stderr)
        sys.exit(1)

    rows = asyncio.run(run_diff(symbols))
    mismatches = [r for r in rows if r.verdict not in {"agree", "bot_no_setup", "no_bot_tick"}]
    report_path = save_diff_report(
        rows,
        meta={
            "ts": datetime.now(UTC).isoformat(),
            "symbols": list(symbols),
            "mismatch_count": len(mismatches),
            "jsonl": str(TICK_JSONL),
        },
    )

    if args.json:
        print(json.dumps(json.loads(report_path.read_text()), indent=2))
    else:
        print(format_diff_table(rows))
        print()
        print(f"mismatches: {len(mismatches)}/{len(rows)}")
        print(f"report: {report_path}")
        for r in mismatches:
            print(
                f"  ! {r.symbol}: {r.verdict} | bot {r.bot_phase}/{r.bot_bias} "
                f"short={r.bot_short} long={r.bot_long} | ind {r.ind_bias} "
                f"({r.ind_score_short}/{r.ind_score_long})"
            )


if __name__ == "__main__":
    main()
