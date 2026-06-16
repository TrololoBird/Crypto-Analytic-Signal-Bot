#!/usr/bin/env python3
"""Independent CCXT + Polars probe (Hunt autonomous loop — ФАЗА 5).

НЕ импортирует hunt runtime. Публичные данные только через CCXT (sync), как в
``hunt_core.market.factory.fetch_klines_sync`` — для сравнения с hunt path.

Запуск:
    .venv/bin/python scripts/hunt_probe_independent.py BEATUSDT VELVETUSDT PLAYUSDT
    .venv/bin/python scripts/hunt_probe_independent.py --watchlist --top 5
    .venv/bin/python scripts/hunt_probe_independent.py BEATUSDT --interval 5m --limit 200

Вывод — компактный JSONL (одна строка на символ) в stdout.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from pathlib import Path

import polars as pl

ROOT = Path(__file__).resolve().parent.parent
HUNT = ROOT / "hunt"
if str(HUNT) not in sys.path:
    sys.path.insert(0, str(HUNT))

from hunt_core.market.factory import (  # noqa: E402
    close_exchange_sync,
    create_sync_binance_future,
    fetch_klines_sync,
)
from hunt_core.market.symbols import to_ccxt_symbol  # noqa: E402

WATCHLIST = ROOT / "hunt" / "data" / "hunt_watchlist.json"
WARMUP_MIN = 60


class ProbeError(RuntimeError):
    """Громкая ошибка зонда: данные недостаточны/битые."""


def _fin(name: str, symbol: str, val: float) -> float:
    if val is None or not isinstance(val, (int, float)) or not math.isfinite(val):
        raise ProbeError(
            f"{symbol}: {name}={val!r} не конечно — баг вычисления, не валидное состояние"
        )
    return float(val)


def _ccxt_exchange(proxy: str | None):
    return create_sync_binance_future(
        proxy_url=proxy,
        trust_env=proxy is None,
    )


def fetch_klines(symbol: str, interval: str, limit: int, proxy: str | None) -> pl.DataFrame:
    rows = fetch_klines_sync(
        symbol,
        interval,
        limit=limit,
        proxy_url=proxy,
        trust_env=proxy is None,
    )
    if len(rows) < WARMUP_MIN:
        raise ProbeError(
            f"{symbol}: klines вернул {len(rows)} баров, нужно ≥{WARMUP_MIN}"
        )
    df = pl.DataFrame(
        {
            "open_time": [int(r[0]) for r in rows],
            "open": [float(r[1]) for r in rows],
            "high": [float(r[2]) for r in rows],
            "low": [float(r[3]) for r in rows],
            "close": [float(r[4]) for r in rows],
            "volume": [float(r[5]) for r in rows],
            "quote_vol": [float(r[7]) if len(r) > 7 else 0.0 for r in rows],
        }
    )
    nulls = df.null_count().sum_horizontal().item()
    if nulls:
        raise ProbeError(f"{symbol}: klines содержит {nulls} null после cast")
    return df


def fetch_oi_delta(symbol: str, proxy: str | None) -> float:
    ex = _ccxt_exchange(proxy)
    try:
        ex.load_markets()
        ccxt_sym = to_ccxt_symbol(symbol, exchange=ex)
        raw = ex.fetch_open_interest_history(ccxt_sym, timeframe="5m", limit=12)
        if not isinstance(raw, list) or len(raw) < 2:
            raise ProbeError(f"{symbol}: openInterestHist <2 точек")
        oi = [float(x.get("openInterestAmount") or x.get("openInterest") or 0) for x in raw]
        if oi[0] == 0.0:
            raise ProbeError(f"{symbol}: первая точка OI = 0")
        return _fin("oi_delta_pct", symbol, round((oi[-1] - oi[0]) / oi[0] * 100.0, 3))
    finally:
        close_exchange_sync(ex, label="probe_oi")


def fetch_funding(symbol: str, proxy: str | None) -> float:
    ex = _ccxt_exchange(proxy)
    try:
        ex.load_markets()
        ccxt_sym = to_ccxt_symbol(symbol, exchange=ex)
        raw = ex.fetch_funding_rate(ccxt_sym)
        if not isinstance(raw, dict) or raw.get("fundingRate") is None:
            raise ProbeError(f"{symbol}: fetchFundingRate без fundingRate")
        return _fin("funding_pct", symbol, round(float(raw["fundingRate"]) * 100.0, 5))
    finally:
        close_exchange_sync(ex, label="probe_funding")


def _wilder(expr: pl.Expr, n: int) -> pl.Expr:
    return expr.ewm_mean(alpha=1.0 / n, adjust=False)


def compute(df: pl.DataFrame, symbol: str, n: int = 14) -> dict:
    c, h, lo = pl.col("close"), pl.col("high"), pl.col("low")
    delta = c.diff()
    gain = pl.when(delta > 0).then(delta).otherwise(0.0)
    loss = pl.when(delta < 0).then(-delta).otherwise(0.0)
    tr = pl.max_horizontal(h - lo, (h - c.shift(1)).abs(), (lo - c.shift(1)).abs())
    up = h.diff()
    dn = -lo.diff()
    plus_dm = pl.when((up > dn) & (up > 0)).then(up).otherwise(0.0)
    minus_dm = pl.when((dn > up) & (dn > 0)).then(dn).otherwise(0.0)
    typical = (h + lo + c) / 3.0
    avg_gain = _wilder(gain, n)
    avg_loss = _wilder(loss, n)
    atr = _wilder(tr, n)
    out = df.with_columns(
        rsi=pl.when(avg_loss <= 0.0)
        .then(100.0)
        .otherwise(100.0 - 100.0 / (1.0 + avg_gain / avg_loss)),
        atr=atr,
        _pdi=pl.when(atr <= 0.0).then(0.0).otherwise(100.0 * _wilder(plus_dm, n) / atr),
        _mdi=pl.when(atr <= 0.0).then(0.0).otherwise(100.0 * _wilder(minus_dm, n) / atr),
        vwap=(typical * pl.col("volume")).cum_sum() / pl.col("volume").cum_sum(),
    )
    di_sum = pl.col("_pdi") + pl.col("_mdi")
    out = out.with_columns(
        _dx=pl.when(di_sum <= 0.0)
        .then(0.0)
        .otherwise(100.0 * (pl.col("_pdi") - pl.col("_mdi")).abs() / di_sum),
    ).with_columns(adx=_wilder(pl.col("_dx"), n))

    tail = out.tail(1).row(0, named=True)
    win = out.tail(60)
    hi = win["high"].max()
    low = win["low"].min()
    rng = hi - low
    close = _fin("close", symbol, tail["close"])
    atr_v = _fin("atr", symbol, tail["atr"])
    vwap_v = _fin("vwap", symbol, tail["vwap"])
    vol_now = win["volume"].tail(3).mean()
    vol_base = win["volume"].mean()

    if close <= 0.0:
        raise ProbeError(f"{symbol}: close={close} ≤ 0")
    if atr_v <= 0.0:
        raise ProbeError(f"{symbol}: ATR={atr_v} ≤ 0")
    if rng <= 0.0:
        raise ProbeError(f"{symbol}: диапазон 60 баров = {rng} ≤ 0")
    if not vol_base or vol_base <= 0.0:
        raise ProbeError(f"{symbol}: средний объём={vol_base} ≤ 0")

    rsi_series = out["rsi"].tail(4).to_list()
    for i, v in enumerate(rsi_series):
        _fin(f"rsi[t-{3 - i}]", symbol, v)

    return {
        "close": round(close, 8),
        "rsi": round(_fin("rsi", symbol, tail["rsi"]), 1),
        "rsi_slope": round(rsi_series[-1] - rsi_series[0], 1),
        "atr_pct": round(_fin("atr_pct", symbol, atr_v / close * 100.0), 3),
        "adx": round(_fin("adx", symbol, tail["adx"]), 1),
        "pdi_minus_mdi": round(
            _fin("pdi", symbol, tail["_pdi"]) - _fin("mdi", symbol, tail["_mdi"]), 1
        ),
        "vwap_dev_atr": round(_fin("vwap_dev", symbol, (close - vwap_v) / atr_v), 2),
        "pos_in_range": round(_fin("pos", symbol, (close - low) / rng), 2),
        "vol_expansion_x": round(_fin("vol_exp", symbol, vol_now / vol_base), 2),
    }


def verdict(m: dict, oi_delta: float) -> tuple[str, str]:
    rsi, slope = m["rsi"], m["rsi_slope"]
    pos, vdev, vol, adx = m["pos_in_range"], m["vwap_dev_atr"], m["vol_expansion_x"], m["adx"]
    oi = oi_delta
    if pos <= 0.35 and slope >= 6 and vol >= 1.4 and rsi < 60 and oi >= 0:
        return "long", f"pos={pos} rsi_slope={slope} vol={vol}x oi={oi}%"
    if pos >= 0.85 and (rsi >= 70 or vdev >= 2.0) and slope <= 2:
        return "short", f"pos={pos} rsi={rsi} vwap_dev={vdev}atr slope={slope}"
    if pos >= 0.8 and oi < -1.0 and slope < 0:
        return "short", f"oi_div oi={oi}% pos={pos} slope={slope}"
    return "none", f"pos={pos} rsi={rsi} adx={adx} no-edge"


def watchlist_symbols(top: int) -> list[str]:
    if not WATCHLIST.exists():
        raise ProbeError(f"watchlist не найден: {WATCHLIST}")
    d = json.loads(WATCHLIST.read_text())
    wl = d.get("watchlist")
    if not wl:
        raise ProbeError(f"{WATCHLIST}: пустой watchlist")
    return [r["symbol"] for r in wl[:top] if r.get("symbol")]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("symbols", nargs="*")
    ap.add_argument("--watchlist", action="store_true")
    ap.add_argument("--top", type=int, default=5)
    ap.add_argument("--interval", default="1m")
    ap.add_argument("--limit", type=int, default=180)
    ap.add_argument("--proxy", default=os.environ.get("HTTPS_PROXY"))
    args = ap.parse_args()

    symbols = list(args.symbols)
    if args.watchlist or not symbols:
        symbols += watchlist_symbols(args.top)
    symbols = list(dict.fromkeys(symbols))
    if not symbols:
        raise ProbeError("не задано ни одного символа")

    failures: list[str] = []
    for sym in symbols:
        try:
            df = fetch_klines(sym, args.interval, args.limit, args.proxy)
            m = compute(df, sym)
            oi = fetch_oi_delta(sym, args.proxy)
            fund = fetch_funding(sym, args.proxy)
            v, why = verdict(m, oi)
            rec = {
                "symbol": sym,
                "interval": args.interval,
                "probe_verdict": v,
                "why": why,
                "oi_delta_pct": oi,
                "funding_pct": fund,
                **m,
            }
            print(json.dumps(rec, allow_nan=False))
            sys.stdout.flush()
        except ProbeError as exc:
            print(f"PROBE_FAIL {sym}: {exc}", file=sys.stderr)
            failures.append(sym)
    if failures:
        print(
            f"PROBE_FAIL_SUMMARY: {len(failures)}/{len(symbols)} провалились: {failures}",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
