"""Historical pump/dump event study at scale — do features PRECEDE moves?

The scientific question for both Hunt modules: are pre-move features present
*before* large moves materially more than at random times (base rate)? A
feature common on pumps but equally common everywhere has no predictive power,
however elegant its formula.

Method (no leakage — features at bar i use only bars <= i; label uses i+1..):

  1. Discover a liquid universe (top-N USDⓈ-M perps by 24h quote volume) or use
     an explicit --symbols list.
  2. Uniformly sample bars at a stride; for each, record pre-move features and a
     binary label is_event = (24h forward favorable excursion >= threshold).
     Uniform sampling (not "all events + sparse control") gives an HONEST base
     rate and lets us compute lift, not just AUC.
  3. Per feature report:
       AUC   — Mann-Whitney separation event vs non-event (0.5 = noise).
       lift  — P(event | feature in top tercile) / base_rate.
       regime stability — AUC recomputed within BTC bull / bear / sideways.

BACKTESTABILITY BOUNDARY: only kline-derivable features here. OI / long-short
ratio have ~30d Binance retention; DOM / orderbook / absorption / liquidity-wall
features have NO historical archive — never validatable retrospectively, only
forward via the outcome tracker.

    .venv/bin/python -m hunt_core._dev.event_study --side pump --top 200 --days 180
    .venv/bin/python -m hunt_core._dev.event_study --side dump --top 300 --days 365 --interval 1h

100% CCXT market plane. Public data only.
"""
from __future__ import annotations

import argparse
import random
import statistics
import time
from typing import Any

from hunt_core.market.factory import (
    close_exchange_sync,
    create_sync_binance_future,
    fetch_klines_sync,
)

_FWD_HOURS = 24                 # forward window for the event label
_FEATURES = ("ret_1h", "ret_4h", "ret_24h", "atr_pct", "compression", "vol_ratio", "range_pos", "rs_vs_btc")
_INTERVAL_MIN = {"15m": 15, "30m": 30, "1h": 60, "2h": 120, "4h": 240}


def _discover_universe(top_n: int, proxy_url: str | None, trust_env: bool) -> list[str]:
    ex = create_sync_binance_future(proxy_url=proxy_url, trust_env=trust_env)
    try:
        ex.load_markets()
        tickers = ex.fetch_tickers()
        rows: list[tuple[str, float]] = []
        for sym, t in tickers.items():
            m = ex.markets.get(sym) or {}
            if not (m.get("swap") and m.get("linear") and m.get("quote") == "USDT" and m.get("active")):
                continue
            qv = float(t.get("quoteVolume") or 0)
            base = (m.get("id") or "").upper()  # e.g. BTCUSDT
            if base.endswith("USDT") and qv > 0:
                rows.append((base, qv))
        rows.sort(key=lambda r: -r[1])
        return [b for b, _ in rows[:top_n]]
    finally:
        close_exchange_sync(ex, label="event_study_discovery")


def _klines(symbol: str, days: int, interval: str, proxy_url: str | None, trust_env: bool) -> list[list[float]]:
    until = int(time.time() * 1000)
    since = until - days * 86400 * 1000
    pages = max(3, days * 24 * 60 // _INTERVAL_MIN[interval] // 1500 + 2)
    raw = fetch_klines_sync(symbol, interval, since_ms=since, until_ms=until,
                            limit=1500, max_pages=pages, proxy_url=proxy_url, trust_env=trust_env)
    return [[float(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5])] for r in raw]


def _atr_pct(bars: list[list[float]], i: int, n: int = 14) -> float:
    if i < n:
        return 0.0
    trs = []
    for j in range(i - n + 1, i + 1):
        hi, lo, pc = bars[j][2], bars[j][3], bars[j - 1][4]
        trs.append(max(hi - lo, abs(hi - pc), abs(lo - pc)))
    close = bars[i][4]
    return (sum(trs) / n) / close * 100.0 if close > 0 else 0.0


def _features_at(bars: list[list[float]], i: int, bpb: int, btc_ret_24h: float | None) -> dict[str, float] | None:
    """Pre-move features using only bars <= i. bpb = bars per hour-block helpers."""
    if i < 48:
        return None
    close = bars[i][4]
    if close <= 0:
        return None
    h1, h4, h24 = bpb, bpb * 4, bpb * 24
    ret_1h = (close - bars[i - h1][4]) / bars[i - h1][4] * 100.0 if i >= h1 and bars[i - h1][4] else 0.0
    ret_4h = (close - bars[i - h4][4]) / bars[i - h4][4] * 100.0 if i >= h4 and bars[i - h4][4] else 0.0
    ret_24h = (close - bars[i - h24][4]) / bars[i - h24][4] * 100.0 if i >= h24 and bars[i - h24][4] else 0.0
    atr_now = _atr_pct(bars, i)
    lo = max(14, i - 24 * 30 * bpb // bpb if False else i - 720)
    atr_hist = [a for a in (_atr_pct(bars, j) for j in range(max(14, i - 720), i, 6)) if a > 0]
    med_atr = statistics.median(atr_hist) if atr_hist else atr_now
    compression = (atr_now / med_atr) if med_atr > 0 else 1.0
    vols = [bars[j][5] for j in range(max(0, i - h24), i)]
    med_vol = statistics.median(vols) if vols else 0.0
    vol_ratio = (bars[i][5] / med_vol) if med_vol > 0 else 1.0
    win = bars[max(0, i - h24):i + 1]
    hi = max(b[2] for b in win); lo_p = min(b[3] for b in win)
    range_pos = (close - lo_p) / (hi - lo_p) if hi > lo_p else 0.5
    rs = (ret_24h - btc_ret_24h) if btc_ret_24h is not None else 0.0
    return {"ret_1h": ret_1h, "ret_4h": ret_4h, "ret_24h": ret_24h, "atr_pct": atr_now,
            "compression": compression, "vol_ratio": vol_ratio, "range_pos": range_pos, "rs_vs_btc": rs}


def _fwd_event(bars: list[list[float]], i: int, side: str, fwd_bars: int, threshold: float) -> bool:
    entry = bars[i][4]
    if entry <= 0:
        return False
    window = bars[i + 1:i + 1 + fwd_bars]
    if len(window) < fwd_bars:
        return False
    if side == "pump":
        return (max(b[2] for b in window) - entry) / entry * 100.0 >= threshold
    return (entry - min(b[3] for b in window)) / entry * 100.0 >= threshold


def _auc(pos: list[float], neg: list[float], cap: int = 4000) -> float:
    """Mann-Whitney AUC with subsampling for tractability at scale."""
    if not pos or not neg:
        return 0.5
    if len(pos) > cap:
        pos = random.sample(pos, cap)
    if len(neg) > cap:
        neg = random.sample(neg, cap)
    neg_sorted = sorted(neg)
    import bisect
    total = 0.0
    for p in pos:
        lo = bisect.bisect_left(neg_sorted, p)
        hi = bisect.bisect_right(neg_sorted, p)
        total += lo + (hi - lo) * 0.5
    return total / (len(pos) * len(neg))


def _regime_series(btc: list[list[float]], bpb: int) -> dict[int, str]:
    """Per-timestamp BTC regime from trailing 7d return: bull/bear/sideways."""
    out: dict[int, str] = {}
    span = 7 * 24 * bpb
    for i in range(len(btc)):
        if i < span:
            out[int(btc[i][0])] = "sideways"
            continue
        r = (btc[i][4] - btc[i - span][4]) / btc[i - span][4] * 100.0 if btc[i - span][4] else 0.0
        out[int(btc[i][0])] = "bull" if r > 8 else "bear" if r < -8 else "sideways"
    return out


def run(side: str, threshold: float, days: int, interval: str, top_n: int,
        symbols: list[str] | None, stride_h: int, proxy_url: str | None, trust_env: bool) -> None:
    bpb = max(1, 60 // _INTERVAL_MIN[interval])  # bars per hour
    fwd_bars = _FWD_HOURS * bpb
    stride = max(1, stride_h * bpb)

    if symbols is None:
        print(f"Discovering top-{top_n} liquid USDⓈ-M perps by 24h volume…")
        symbols = _discover_universe(top_n, proxy_url, trust_env)
        print(f"  universe = {len(symbols)} symbols")

    btc = _klines("BTCUSDT", days, interval, proxy_url, trust_env)
    btc_ret = {}
    h24 = 24 * bpb
    for i in range(h24, len(btc)):
        btc_ret[int(btc[i][0])] = (btc[i][4] - btc[i - h24][4]) / btc[i - h24][4] * 100.0 if btc[i - h24][4] else 0.0
    regime = _regime_series(btc, bpb)

    # samples[feature] = list of (value, is_event, regime)
    feat_vals: dict[str, list[float]] = {f: [] for f in _FEATURES}
    is_event: list[bool] = []
    regimes: list[str] = []
    n_ok = 0
    t0 = time.time()
    for idx, sym in enumerate(symbols, 1):
        try:
            bars = _klines(sym, days, interval, proxy_url, trust_env)
        except Exception as exc:  # noqa: BLE001
            print(f"  ! {sym} fetch failed: {exc}")
            continue
        if len(bars) < 100:
            continue
        n_ok += 1
        for i in range(48, len(bars) - fwd_bars, stride):
            ts = int(bars[i][0])
            feats = _features_at(bars, i, bpb, btc_ret.get(ts))
            if feats is None:
                continue
            ev = _fwd_event(bars, i, side, fwd_bars, threshold)
            for f in _FEATURES:
                feat_vals[f].append(feats[f])
            is_event.append(ev)
            regimes.append(regime.get(ts, "sideways"))
        if idx % 25 == 0:
            print(f"  …{idx}/{len(symbols)} symbols, {len(is_event)} samples, {time.time() - t0:.0f}s")

    n = len(is_event)
    n_ev = sum(is_event)
    if n == 0 or n_ev == 0:
        print(f"No usable samples / events (side={side}, thr={threshold}%, {n_ok} symbols).")
        return
    base = n_ev / n

    print(f"\n=== {side.upper()} event study @ >= {threshold}% in {_FWD_HOURS}h "
          f"({days}d, {interval}, {n_ok} symbols) ===")
    print(f"samples={n}  events={n_ev}  base_rate={base * 100:.2f}%  "
          f"(autocorrelated — significance overstated, point estimates valid)\n")
    print(f"{'feature':13} {'AUC':>5} {'lift':>5} {'ev_mean':>8} {'nv_mean':>8} "
          f"{'bull':>5} {'bear':>5} {'side':>5}  read")
    print("-" * 78)

    for f in _FEATURES:
        vals = feat_vals[f]
        pos = [v for v, e in zip(vals, is_event) if e]
        neg = [v for v, e in zip(vals, is_event) if not e]
        if not pos or not neg:
            continue
        auc = _auc(pos, neg)
        # lift: event rate among top-tercile feature values / base rate
        thr = statistics.quantiles(vals, n=3)[1] if len(vals) >= 3 else statistics.median(vals)
        top_ev = sum(1 for v, e in zip(vals, is_event) if v >= thr and e)
        top_n_samples = sum(1 for v in vals if v >= thr)
        lift = ((top_ev / top_n_samples) / base) if top_n_samples and base else 0.0
        # regime stability
        regime_auc = {}
        for rg in ("bull", "bear", "sideways"):
            rp = [v for v, e, r in zip(vals, is_event, regimes) if e and r == rg]
            rn = [v for v, e, r in zip(vals, is_event, regimes) if not e and r == rg]
            regime_auc[rg] = _auc(rp, rn) if rp and rn else 0.5
        sig = abs(auc - 0.5)
        verdict = "STRONG" if sig >= 0.15 else "useful" if sig >= 0.10 else "weak" if sig >= 0.05 else "NOISE"
        print(f"{f:13} {auc:>5.2f} {lift:>5.2f} {statistics.mean(pos):>8.2f} {statistics.mean(neg):>8.2f} "
              f"{regime_auc['bull']:>5.2f} {regime_auc['bear']:>5.2f} {regime_auc['sideways']:>5.2f}  {verdict}")
    print("-" * 78)
    print("AUC 0.5=noise · 0.55 weak · 0.60 useful · 0.65+ strong.  lift = x base rate.")
    print("regime cols = AUC within BTC bull/bear/sideways — a real feature holds across all three.")
    print("\nNOTE: DOM / orderbook / absorption / liquidity features absent — no historical")
    print("archive; validate those only forward via the outcome tracker.")


def main() -> None:
    ap = argparse.ArgumentParser(description="Scaled historical pump/dump pre-move feature study.")
    ap.add_argument("--side", choices=["pump", "dump"], default="pump")
    ap.add_argument("--threshold", type=float, default=15.0)
    ap.add_argument("--days", type=int, default=180)
    ap.add_argument("--interval", choices=sorted(_INTERVAL_MIN), default="1h")
    ap.add_argument("--top", type=int, default=200, help="Top-N liquid symbols (ignored if --symbols given).")
    ap.add_argument("--symbols", nargs="*", default=None)
    ap.add_argument("--stride-h", type=int, default=3, help="Sample one bar every N hours.")
    ap.add_argument("--proxy-url", default=None)
    ap.add_argument("--no-trust-env", action="store_true")
    args = ap.parse_args()
    random.seed(7)
    run(args.side, args.threshold, args.days, args.interval, args.top,
        args.symbols, args.stride_h, args.proxy_url, not args.no_trust_env)


if __name__ == "__main__":
    main()
