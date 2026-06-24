"""Lake soak progress — bar counts, column fill, quarantine readiness."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone

import polars as pl

from hunt_core._dev.factor_promotion_gate import min_outcomes_for_power, quarantine_factors
from hunt_core.features.feature_engine import load_feature_registry
from hunt_core.paths import LAKE_PARQUET
from hunt_core.scanner.detect.config import fusion_params

GAP_CLOSE_COLS = ("delta_ratio", "zscore30", "session_cvd", "rolling_cvd_24h")
TIER1_COLS = (
    "oi_acceleration",
    "funding_velocity",
    "poc_migration_1h",
    "poc_migration_4h",
    "va_contraction",
    "liquidity_void_path",
)


def _lake_symbols() -> list[str]:
    if not LAKE_PARQUET.exists():
        return []
    out: list[str] = []
    for c in sorted(LAKE_PARQUET.iterdir()):
        if c.is_dir() and not c.name.startswith("symbol=") and (c / "15m.parquet").exists():
            out.append(c.name.upper())
    return out


def _fill_rate(df: pl.DataFrame, col: str) -> float:
    if df.is_empty() or col not in df.columns:
        return 0.0
    s = df[col]
    if s.dtype in (pl.Float64, pl.Float32, pl.Int64, pl.Int32):
        valid = s.is_not_null() & s.is_finite()
    else:
        valid = s.is_not_null()
    return float(valid.sum()) / max(1, df.height)


def symbol_report(symbol: str, *, tf: str = "15m") -> dict[str, object]:
    path = LAKE_PARQUET / symbol.upper() / f"{tf}.parquet"
    if not path.exists():
        return {"symbol": symbol, "rows": 0, "missing": True}
    df = pl.read_parquet(path)
    rows = df.height
    ts_min = ts_max = None
    if rows and "ts" in df.columns:
        try:
            ts_min = str(df["ts"].min())
            ts_max = str(df["ts"].max())
        except Exception:
            pass
    fills = {c: round(_fill_rate(df, c), 3) for c in GAP_CLOSE_COLS + TIER1_COLS if c in df.columns}
    missing_cols = [c for c in GAP_CLOSE_COLS + TIER1_COLS if c not in df.columns]
    return {
        "symbol": symbol,
        "rows": rows,
        "ts_min": ts_min,
        "ts_max": ts_max,
        "fills": fills,
        "missing_cols": missing_cols,
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Lake soak status for fusion/quarantine promotion")
    p.add_argument("--symbol", default="")
    p.add_argument("--json", action="store_true")
    args = p.parse_args(argv)

    fp = fusion_params()
    min_n = int(getattr(fp, "min_n", 30) or 30)
    need_promo = min_outcomes_for_power()
    quarantine = sorted(quarantine_factors())
    registry = load_feature_registry()
    schema_ver = registry.get("schema_version")

    symbols = [args.symbol.upper()] if args.symbol else _lake_symbols()
    if not symbols:
        print(f"lake_soak: empty | path={LAKE_PARQUET} | min_n={min_n} need_promo_n={need_promo}")
        print(f"  quarantine_factors={quarantine}")
        print("  action: run `python -m hunt_core watch --no-telegram` until 15m closed bars accrue")
        return 0

    reports = [symbol_report(s) for s in symbols]
    total_rows = sum(int(r["rows"]) for r in reports)
    ready_min_n = sum(1 for r in reports if int(r["rows"]) >= min_n)
    ready_promo = sum(1 for r in reports if int(r["rows"]) >= need_promo)

    print(f"lake_soak @ {datetime.now(timezone.utc).isoformat(timespec='seconds')}")
    print(f"  path={LAKE_PARQUET} schema_version={schema_ver!r}")
    print(f"  symbols={len(symbols)} total_rows={total_rows} ready_min_n={ready_min_n} ready_promo_n={ready_promo}")
    print(f"  fusion_min_n={min_n} promotion_need_n={need_promo}")
    print(f"  quarantine={quarantine}")

    for r in reports[:20]:
        sym = r["symbol"]
        rows = int(r["rows"])
        flag = "OK" if rows >= min_n else "COLD"
        print(f"  {sym:12} rows={rows:4} [{flag}] fills={r.get('fills') or {}}")
        missing = r.get("missing_cols") or []
        if missing:
            print(f"               missing_cols={missing}")

    if len(reports) > 20:
        print(f"  ... +{len(reports) - 20} more symbols")

    bars_per_day = 96  # 15m bars
    if ready_min_n < len(symbols) and reports:
        max_rows = max(int(r["rows"]) for r in reports)
        need_bars = max(0, min_n - max_rows)
        est_days = need_bars / bars_per_day
        print(f"  est_days_to_min_n (worst symbol): {est_days:.1f}d ({need_bars} bars @ 15m)")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
