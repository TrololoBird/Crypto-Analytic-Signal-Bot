#!/usr/bin/env python3
"""Independent Binance + Polars probe (Hunt autonomous loop — ФАЗА 5, источник 4).

НЕ импортирует hunt_watch. Свои публичные REST-запросы к Binance USDⓈ-M +
пересчёт индикаторов в Polars + независимый вердикт. Сравнивается с тем, что
отдаёт охотник, чтобы ловить баги hunt path (а не подгонять зонд под баг).

Запуск:
    .venv/bin/python scripts/hunt_probe_independent.py BEATUSDT VELVETUSDT PLAYUSDT
    .venv/bin/python scripts/hunt_probe_independent.py --watchlist --top 5
    .venv/bin/python scripts/hunt_probe_independent.py BEATUSDT --interval 5m --limit 200

Вывод — компактный JSONL (одна строка на символ) в stdout. Никакой прозы.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from pathlib import Path

import polars as pl
import requests

FAPI = "https://fapi.binance.com"
ROOT = Path(__file__).resolve().parent.parent
WATCHLIST = ROOT / "hunt" / "data" / "hunt_watchlist.json"


WARMUP_MIN = 60  # минимум баров для устойчивого Wilder/ADX; меньше — отказ, не приближение


class ProbeError(RuntimeError):
    """Громкая ошибка зонда: данные недостаточны/битые. Никогда не глушится."""


def _fin(name: str, symbol: str, val: float) -> float:
    """Гарантия конечного числа. Не конечно → raise (NaN/inf/None запрещены)."""
    if val is None or not isinstance(val, (int, float)) or not math.isfinite(val):
        raise ProbeError(f"{symbol}: {name}={val!r} не конечно — баг вычисления, не валидное состояние")
    return float(val)


def _get(path: str, params: dict, proxy: str | None) -> object:
    """Транзиентные сетевые ошибки ретраятся; финальный провал — raise с диагностикой."""
    proxies = {"https": proxy, "http": proxy} if proxy else None
    last: Exception | None = None
    for attempt in range(3):
        try:
            r = requests.get(FAPI + path, params=params, timeout=10, proxies=proxies)
            r.raise_for_status()
            return r.json()
        except requests.RequestException as exc:
            last = exc
            time.sleep(0.4 * (attempt + 1))
    raise ProbeError(f"GET {path} {params} провалился после 3 попыток: {last!r}")


def fetch_klines(symbol: str, interval: str, limit: int, proxy: str | None) -> pl.DataFrame:
    raw = _get("/fapi/v1/klines", {"symbol": symbol, "interval": interval, "limit": limit}, proxy)
    if not isinstance(raw, list) or len(raw) < WARMUP_MIN:
        raise ProbeError(f"{symbol}: klines вернул {len(raw) if isinstance(raw, list) else type(raw)} баров, нужно ≥{WARMUP_MIN}")
    cols = ["open_time", "open", "high", "low", "close", "volume", "close_time",
            "quote_vol", "trades", "taker_base", "taker_quote", "ignore"]
    df = pl.DataFrame(raw, schema=cols, orient="row").select(
        pl.col("open_time").cast(pl.Int64),
        *[pl.col(c).cast(pl.Float64) for c in ("open", "high", "low", "close", "volume", "quote_vol")],
    )
    nulls = df.null_count().sum_horizontal().item()
    if nulls:
        raise ProbeError(f"{symbol}: klines содержит {nulls} null после cast — битые данные")
    return df


def fetch_oi_delta(symbol: str, proxy: str | None) -> float:
    raw = _get("/futures/data/openInterestHist",
               {"symbol": symbol, "period": "5m", "limit": 12}, proxy)
    if not isinstance(raw, list) or len(raw) < 2:
        raise ProbeError(f"{symbol}: openInterestHist вернул <2 точек — OI недоступен (не None, а явная ошибка)")
    oi = [float(x["sumOpenInterest"]) for x in raw]
    if oi[0] == 0.0:
        raise ProbeError(f"{symbol}: первая точка OI = 0, деление невозможно")
    return _fin("oi_delta_pct", symbol, round((oi[-1] - oi[0]) / oi[0] * 100.0, 3))


def fetch_funding(symbol: str, proxy: str | None) -> float:
    raw = _get("/fapi/v1/premiumIndex", {"symbol": symbol}, proxy)
    if not isinstance(raw, dict) or "lastFundingRate" not in raw:
        raise ProbeError(f"{symbol}: premiumIndex без lastFundingRate — funding недоступен")
    return _fin("funding_pct", symbol, round(float(raw["lastFundingRate"]) * 100.0, 5))


def _wilder(expr: pl.Expr, n: int) -> pl.Expr:
    # Wilder smoothing == EWM с alpha = 1/n, adjust=False.
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
        # avg_loss==0 → RSI=100 по определению (корректный предел, не маскировка)
        rsi=pl.when(avg_loss <= 0.0).then(100.0)
            .otherwise(100.0 - 100.0 / (1.0 + avg_gain / avg_loss)),
        atr=atr,
        _pdi=pl.when(atr <= 0.0).then(0.0).otherwise(100.0 * _wilder(plus_dm, n) / atr),
        _mdi=pl.when(atr <= 0.0).then(0.0).otherwise(100.0 * _wilder(minus_dm, n) / atr),
        vwap=(typical * pl.col("volume")).cum_sum() / pl.col("volume").cum_sum(),
    )
    di_sum = pl.col("_pdi") + pl.col("_mdi")
    out = out.with_columns(
        _dx=pl.when(di_sum <= 0.0).then(0.0)
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

    # Дегенеративные знаменатели — явная ошибка, не подстановка epsilon
    if close <= 0.0:
        raise ProbeError(f"{symbol}: close={close} ≤ 0")
    if atr_v <= 0.0:
        raise ProbeError(f"{symbol}: ATR={atr_v} ≤ 0 (плоский рынок) — индикаторы не определены")
    if rng <= 0.0:
        raise ProbeError(f"{symbol}: диапазон 60 баров = {rng} ≤ 0 — позиция не определена")
    if not vol_base or vol_base <= 0.0:
        raise ProbeError(f"{symbol}: средний объём={vol_base} ≤ 0 — vol_expansion не определён")

    rsi_series = out["rsi"].tail(4).to_list()
    for i, v in enumerate(rsi_series):
        _fin(f"rsi[t-{3-i}]", symbol, v)

    return {
        "close": round(close, 8),
        "rsi": round(_fin("rsi", symbol, tail["rsi"]), 1),
        "rsi_slope": round(rsi_series[-1] - rsi_series[0], 1),
        "atr_pct": round(_fin("atr_pct", symbol, atr_v / close * 100.0), 3),
        "adx": round(_fin("adx", symbol, tail["adx"]), 1),
        "pdi_minus_mdi": round(_fin("pdi", symbol, tail["_pdi"]) - _fin("mdi", symbol, tail["_mdi"]), 1),
        "vwap_dev_atr": round(_fin("vwap_dev", symbol, (close - vwap_v) / atr_v), 2),
        "pos_in_range": round(_fin("pos", symbol, (close - low) / rng), 2),
        "vol_expansion_x": round(_fin("vol_exp", symbol, vol_now / vol_base), 2),
    }


def verdict(m: dict, oi_delta: float) -> tuple[str, str]:
    """Прозрачная эвристика: pump-start(long) / dump|exhaustion(short) / none.
    Все входы уже гарантированно конечны (compute/_fin) — никаких `or 0.0`."""
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
    ap.add_argument("--watchlist", action="store_true", help="use top of hunt watchlist")
    ap.add_argument("--top", type=int, default=5)
    ap.add_argument("--interval", default="1m")
    ap.add_argument("--limit", type=int, default=180)
    ap.add_argument("--proxy", default=os.environ.get("HTTPS_PROXY"))
    args = ap.parse_args()

    symbols = list(args.symbols)
    if args.watchlist or not symbols:
        symbols += watchlist_symbols(args.top)
    symbols = list(dict.fromkeys(symbols))  # dedupe, keep order
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
            rec = {"symbol": sym, "interval": args.interval, "probe_verdict": v,
                   "why": why, "oi_delta_pct": oi, "funding_pct": fund, **m}
            # Финальный контракт: каждое число конечно — иначе строку не выпускаем.
            print(json.dumps(rec, allow_nan=False))
            sys.stdout.flush()
        except ProbeError as exc:
            # Громко: stderr + регистрируем провал, НЕ выдаём фейковую строку в stdout.
            print(f"PROBE_FAIL {sym}: {exc}", file=sys.stderr)
            failures.append(sym)
    if failures:
        print(f"PROBE_FAIL_SUMMARY: {len(failures)}/{len(symbols)} провалились: {failures}",
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
