"""Smoke for scanner lake_panel over parquet (not Module 1 /signal).

Run as ``python -m hunt_core._dev.check_deep [--symbol SYM]``. Builds the fusion lake
report for each symbol and prints formatted output. Verifies determinism on lake data.
Operator ``/signal`` uses ``hunt_core.deep`` (verdict_v2), not this path.
"""
from __future__ import annotations

import argparse
import sys

from hunt_core.scanner.detect.lake_panel import build_deep_report_from_lake
from hunt_core.paths import LAKE_PARQUET


def _lake_symbols() -> list[str]:
    if not LAKE_PARQUET.exists():
        return []
    return [
        c.name
        for c in sorted(LAKE_PARQUET.iterdir())
        if c.is_dir() and not c.name.startswith("symbol=") and (c / "15m.parquet").exists()
    ]


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Deep-analysis smoke over the lake")
    p.add_argument("--symbol", default="")
    args = p.parse_args(argv)

    symbols = [args.symbol.upper()] if args.symbol else _lake_symbols()
    if not symbols:
        print("no lake parquet under", LAKE_PARQUET)
        return 1

    for sym in symbols:
        rep = build_deep_report_from_lake(sym)
        if rep is None:
            print(f"\n== {sym}: no data ==")
            continue
        rep2 = build_deep_report_from_lake(sym)
        if rep.summary() != rep2.summary():
            print(f"\n== {sym}: NON-DETERMINISTIC {rep.summary()} != {rep2.summary()}")
            return 1
        print("\n" + "=" * 60)
        print(rep.text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
