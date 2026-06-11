#!/usr/bin/env python3
"""Independent BEAT analysis — raw REST klines, no bot hunt heuristics."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from hunt_watch.bootstrap import bootstrap

bootstrap()

import polars as pl

from engine.domain.config import load_settings
from engine.features.prepare_frame import _prepare_frame
from engine.market.data import BinanceFuturesMarketData
from engine.market.rest_impl import BinanceClientImpl


def _wilder_rsi(closes: list[float], period: int = 14) -> float | None:
    if len(closes) < period + 2:
        return None
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))
    avg_g = sum(gains[:period]) / period
    avg_l = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_g = (avg_g * (period - 1) + gains[i]) / period
        avg_l = (avg_l * (period - 1) + losses[i]) / period
    if avg_l == 0:
        return 100.0
    rs = avg_g / avg_l
    return 100.0 - 100.0 / (1.0 + rs)


def _tf_summary(df: pl.DataFrame | None, label: str) -> dict[str, Any]:
    if df is None or df.is_empty():
        return {"tf": label, "status": "empty"}
    raw_closes = [float(x) for x in df["close"].to_list()]
    work = _prepare_frame(df)
    closes = [float(x) for x in work["close"].to_list()] if not work.is_empty() else raw_closes
    if not closes:
        return {"tf": label, "status": "empty"}
    last = closes[-1]
    prev = closes[-2] if len(closes) >= 2 else last
    closed = work.filter(pl.col("is_closed") == True) if "is_closed" in work.columns else work  # noqa: E712
    c_close = float(closed["close"][-1]) if closed.height else last
    rsi_live = _wilder_rsi(closes)
    rsi_closed = _wilder_rsi(closes[:-1]) if len(closes) > 15 else rsi_live
    hi48 = max(
        float(x)
        for x in (work["high"].to_list() if not work.is_empty() else df["high"].to_list())[-48:]
    )
    lo48 = min(
        float(x)
        for x in (work["low"].to_list() if not work.is_empty() else df["low"].to_list())[-48:]
    )
    return {
        "tf": label,
        "bars": work.height,
        "last": round(last, 4),
        "prev_close": round(prev, 4),
        "last_closed": round(c_close, 4),
        "change_last_pct": round((last / prev - 1) * 100, 3) if prev else None,
        "rsi14_live": round(rsi_live, 1) if rsi_live else None,
        "rsi14_closed": round(rsi_closed, 1) if rsi_closed else None,
        "swing48h_high": round(hi48, 4),
        "swing48h_low": round(lo48, 4),
        "above_swing_mid": last > (hi48 + lo48) / 2,
    }


async def analyze(symbol: str = "BEATUSDT") -> dict[str, Any]:
    settings = load_settings()
    client = BinanceFuturesMarketData(
        binance_client=BinanceClientImpl(
            rest_timeout_seconds=45.0,
            futures_data_request_limit_per_5m=settings.runtime.futures_data_request_limit_per_5m,
            proxy_url=settings.network.proxy_url,
            trust_env=settings.network.trust_env,
        ),
    )
    try:
        limits = {"1m": 500, "5m": 200, "15m": 120, "1h": 100, "4h": 80}
        klines = {
            tf: await client.fetch_klines_cached(symbol, tf, limit=lim)
            for tf, lim in limits.items()
        }
        ticker_rows = await client.fetch_ticker_24h()
        ticker = next((r for r in ticker_rows if r.get("symbol") == symbol), {})
        oi = await client.fetch_open_interest(symbol)
        oi_1h = await client.fetch_open_interest_change(symbol, period="1h")
        taker_5m = await client.fetch_taker_ratio(symbol, period="5m")
        funding = await client.fetch_funding_rate(symbol)

        price = float(ticker.get("last_price") or 0)
        high_24h = float(ticker.get("high_price") or 0)
        low_24h = float(ticker.get("low_price") or 0)
        pos = (price - low_24h) / (high_24h - low_24h) if high_24h > low_24h else 0.5

        tf_data = {tf: _tf_summary(klines[tf], tf) for tf in limits}
        m5, m15, h1 = tf_data["5m"], tf_data["15m"], tf_data["1h"]

        # Key levels (independent)
        swing_high = h1["swing48h_high"]
        swing_low = h1["swing48h_low"]
        bot_style_support = round(swing_high * 0.998, 4)  # mirrors hunt bug/feature

        # Bounce from session low
        m1 = tf_data["1m"]
        recent_low_1h = (
            min(float(x) for x in klines["1m"]["low"].to_list()[-60:])
            if klines["1m"] is not None
            else None
        )
        bounce_pct = ((price / recent_low_1h - 1) * 100) if recent_low_1h else None

        # Independent verdict
        reasons_short: list[str] = []
        reasons_long: list[str] = []
        score_short = 0
        score_long = 0

        if price < bot_style_support:
            score_short += 20
            reasons_short.append(f"still_below_impulse_high_support_{bot_style_support}")
        else:
            score_long += 25
            reasons_long.append(f"reclaimed_above_{bot_style_support}")

        if m5.get("change_last_pct", 0) > 0.15:
            score_long += 15
            reasons_long.append("5m_momentum_up")
        if m15.get("rsi14_closed", 50) and m15["rsi14_closed"] > 55:
            score_long += 10
            reasons_long.append(f"15m_rsi_closed={m15['rsi14_closed']}")
        if m5.get("rsi14_live", 50) and m5["rsi14_live"] > 58:
            score_long += 10
            reasons_long.append(f"5m_rsi_live={m5['rsi14_live']}")

        if h1.get("rsi14_live", 50) and h1["rsi14_live"] > 60:
            score_long += 12
            reasons_long.append("1h_rsi_rising")
        if pos > 0.55:
            score_long += 10
            reasons_long.append(f"pos_in_24h_range={pos:.2f}")

        if taker_5m and taker_5m > 1.05:
            score_long += 8
            reasons_long.append(f"taker_5m_buy={taker_5m:.3f}")
        elif taker_5m and taker_5m < 0.98:
            score_short += 8
            reasons_short.append(f"taker_5m_sell={taker_5m:.3f}")

        if bounce_pct and bounce_pct >= 2.0:
            score_long += 15
            reasons_long.append(f"bounce_from_1h_low={bounce_pct:.1f}%")

        if h1.get("above_swing_mid"):
            score_long += 10
            reasons_long.append("price_above_1h_swing_mid")

        # Sticky confirm problem: old break below 4.98 doesn't mean still short if reclaiming
        closed_5m = m5.get("last_closed", 0)
        closed_15m = m15.get("last_closed", 0)
        still_broken = closed_5m < bot_style_support and closed_15m < bot_style_support

        if still_broken:
            score_short += 15
            reasons_short.append("closed_bars_still_below_old_support")
        else:
            score_long += 20
            reasons_long.append("5m_or_15m_closed_reclaimed_support")

        bias = "neutral"
        if score_long > score_short + 10:
            bias = "long_bounce / invalid_short"
        elif score_short > score_long + 10:
            bias = "short_still_valid"
        else:
            bias = "mixed_chop"

        return {
            "ts": datetime.now(UTC).isoformat(),
            "symbol": symbol,
            "price": price,
            "change_24h_pct": round(float(ticker.get("price_change_percent") or 0), 2),
            "high_24h": high_24h,
            "low_24h": low_24h,
            "pos_in_range_24h": round(pos, 3),
            "oi": oi,
            "oi_chg_1h": oi_1h,
            "taker_5m": taker_5m,
            "funding_pct": round(float(funding or 0) * 100, 4),
            "levels": {
                "swing48h_high": swing_high,
                "swing48h_low": swing_low,
                "bot_support_break": bot_style_support,
                "recent_1h_low": round(recent_low_1h, 4) if recent_low_1h else None,
                "bounce_from_1h_low_pct": round(bounce_pct, 2) if bounce_pct else None,
            },
            "timeframes": tf_data,
            "independent": {
                "score_short": score_short,
                "score_long": score_long,
                "reasons_short": reasons_short,
                "reasons_long": reasons_long,
                "closed_still_below_old_support": still_broken,
                "bias": bias,
            },
        }
    finally:
        await client.close()


def main() -> None:
    result = asyncio.run(analyze())
    out = Path("data/snapshots/beat_independent.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=2), encoding="utf-8")
    ind = result["independent"]
    lv = result["levels"]
    print("=== INDEPENDENT BEAT ANALYSIS ===")
    print(
        f"price {result['price']} | 24h {result['change_24h_pct']}% | pos {result['pos_in_range_24h']}"
    )
    print(
        f"levels: swing {lv['swing48h_low']}-{lv['swing48h_high']} | bot_support {lv['bot_support_break']}"
    )
    print(f"bounce from 1h low: {lv['bounce_from_1h_low_pct']}%")
    print(
        f"5m closed {result['timeframes']['5m'].get('last_closed')} | 15m closed {result['timeframes']['15m'].get('last_closed')}"
    )
    print(f"still broken (closed): {ind['closed_still_below_old_support']}")
    print(f"VERDICT: {ind['bias']} | short={ind['score_short']} long={ind['score_long']}")
    print("long reasons:", ind["reasons_long"])
    print("short reasons:", ind["reasons_short"])


if __name__ == "__main__":
    main()
