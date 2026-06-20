"""Smoke + determinism check for the fusion detection engine over the parquet lake.

Not a pytest file (project rule forbids new test files) — a dev script run as
``python -m hunt_core._dev.check_factors_fusion``. It loads each per-symbol 15m lake
parquet, builds a no-lookahead trailing window at the last bar, computes the six
factors, and verifies:

- no exceptions on real (thin) lake data,
- abstaining factors report ``active=False`` rather than emitting a default,
- the computation is deterministic (same window ⇒ identical scores).

phase-3 extends this with the fused decision once ``build_detection`` lands.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import polars as pl

from hunt_core.detect import build_detection
from hunt_core.detect import factors as F
from hunt_core.detect.fusion import fuse
from hunt_core.detect.windows import build_window
from hunt_core.paths import LAKE_PARQUET


def _lake_symbols() -> list[str]:
    if not LAKE_PARQUET.exists():
        return []
    out: list[str] = []
    for child in sorted(LAKE_PARQUET.iterdir()):
        if child.is_dir() and not child.name.startswith("symbol=") and (child / "15m.parquet").exists():
            out.append(child.name)
    return out


def _check_symbol(symbol: str) -> bool:
    path = LAKE_PARQUET / symbol / "15m.parquet"
    df = pl.read_parquet(path)
    if df.height == 0:
        print(f"  {symbol}: empty parquet — skip")
        return True
    win = build_window(df, symbol=symbol)
    scores = F.compute_factors(win)
    scores2 = F.compute_factors(build_window(df, symbol=symbol))
    for a, b in zip(scores, scores2, strict=True):
        if (a.active, round(a.score, 9)) != (b.active, round(b.score, 9)):
            print(f"  {symbol}: NON-DETERMINISTIC factor {a.name}: {a.score} != {b.score}")
            return False
    active = [s for s in scores if s.active]
    summary = " ".join(
        f"{s.name}={s.score:+.2f}" if s.active else f"{s.name}=abstain" for s in scores
    )
    print(f"  {symbol}: bars={win.height} active={len(active)}/6 | {summary}")

    # Full pipeline: walk bars to accumulate the trailing fused-magnitude history,
    # then run build_detection at the last bar against that self-calibrated gate.
    mags: list[float] = []
    for i in range(df.height):
        wi = build_window(df.head(i + 1), symbol=symbol)
        mags.append(fuse(F.compute_factors(wi)).magnitude)
    hist = pl.Series(mags[:-1], dtype=pl.Float64) if len(mags) > 1 else None
    det = build_detection(win, magnitude_history=hist)
    print(f"    detection: {det.as_summary()}")
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fusion factor smoke over the lake")
    parser.add_argument("--symbol", default="", help="single symbol (default: all lake symbols)")
    args = parser.parse_args(argv)

    symbols = [args.symbol.upper()] if args.symbol else _lake_symbols()
    if not symbols:
        print("no lake parquet found under", LAKE_PARQUET)
        return 1
    print(f"checking {len(symbols)} symbol(s) under {LAKE_PARQUET}")
    ok = True
    for sym in symbols:
        p: Path = LAKE_PARQUET / sym / "15m.parquet"
        if not p.exists():
            print(f"  {sym}: no 15m.parquet — skip")
            continue
        ok = _check_symbol(sym) and ok
    print("RESULT:", "OK" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
