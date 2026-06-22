"""Smoke check for quarantine (shadow) fusion factors over the parquet lake."""
from __future__ import annotations

import argparse
import sys

from hunt_core._dev.check_factors_fusion import _check_symbol, _lake_symbols
from hunt_core.paths import LAKE_PARQUET
from hunt_core.scanner.detect.factors_quarantine import compute_quarantine_factors
from hunt_core.scanner.detect.windows import build_window

import polars as pl


def _check_quarantine_symbol(symbol: str) -> bool:
    path = LAKE_PARQUET / symbol / "15m.parquet"
    if not path.exists():
        print(f"  {symbol}: no 15m.parquet — skip")
        return True
    df = pl.read_parquet(path)
    if df.height == 0:
        return True
    win = build_window(df, symbol=symbol)
    scores = compute_quarantine_factors(win)
    active = [s for s in scores if s.active]
    summary = " ".join(
        f"{s.name}={s.score:+.2f}" if s.active else f"{s.name}=abstain" for s in scores
    )
    print(f"  {symbol}: quarantine active={len(active)}/{len(scores)} | {summary}")
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Quarantine factor smoke over the lake")
    parser.add_argument("--symbol", default="", help="single symbol (default: all)")
    args = parser.parse_args(argv)
    symbols = [args.symbol.upper()] if args.symbol else _lake_symbols()
    if not symbols:
        print("no lake parquet found under", LAKE_PARQUET)
        return 1
    print(f"check_quarantine_factors | {len(symbols)} symbol(s)")
    ok = all(_check_quarantine_symbol(sym) for sym in symbols)
    # production pipeline still must pass
    for sym in symbols:
        ok = _check_symbol(sym) and ok
    print("RESULT:", "OK" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
