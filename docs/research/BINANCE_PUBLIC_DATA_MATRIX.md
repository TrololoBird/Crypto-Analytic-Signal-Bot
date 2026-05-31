# Binance USD-M Public Data Matrix

**Security type: NONE** — no API key. Exclude TRADE, USER_DATA, USER_STREAM, signed routes, `/private` WS.

Base REST: `https://fapi.binance.com` — [General Info](https://developers.binance.com/docs/derivatives/usds-margined-futures/general-info)  
WS: [Market Streams](https://developers.binance.com/docs/derivatives/usds-margined-futures/websocket-market-streams)

## Rate limits

| Limit | Value | Source |
|-------|-------|--------|
| REQUEST_WEIGHT | 2400 / minute | exchangeInfo `rateLimits` |
| 429 → backoff; 418 ban | General Info |
| `/futures/data/*` | 1000 req / 5 min / IP | OI Statistics docs |
| WS streams / connection | ≤ 1024 | Connect |
| WS incoming messages | ≤ 10 / sec | Connect |
| WS reconnect | every 24h | Connect |
| `GET /fapi/v1/klines` weight | 1–10 by limit | Klines doc |

## REST `/fapi/v1/*` (market data)

| Endpoint | Weight (typical) | Use in signal-bot |
|----------|------------------|-------------------|
| `GET /fapi/v1/ping` | 1 | Health |
| `GET /fapi/v1/time` | 1 | Clock sync |
| `GET /fapi/v1/exchangeInfo` | 1 | Symbols, filters |
| `GET /fapi/v1/klines` | 1–10 | OHLCV MTF cache |
| `GET /fapi/v1/markPriceKlines` | 1–10 | Mark-based TA |
| `GET /fapi/v1/indexPriceKlines` | 1–10 | Index TA |
| `GET /fapi/v1/ticker/24hr` | 1 / ~40 all | Universe volume |
| `GET /fapi/v1/ticker/bookTicker` | 1–2 | Spread |
| `GET /fapi/v1/premiumIndex` | 1–10 | Mark, funding |
| `GET /fapi/v1/fundingRate` | 1 | Funding history |
| `GET /fapi/v1/fundingInfo` | 1 | Funding rules |
| `GET /fapi/v1/depth` | 2–20 | L2 snapshot |
| `GET /fapi/v1/aggTrades` | 20 | CVD backfill |
| `GET /fapi/v1/openInterest` | 1 | OI snapshot |
| `GET /fapi/v1/ticker/price` | 1–2 | Fallback price |
| `GET /fapi/v1/constituents` | 2 | Index basket |

## REST `/futures/data/*` (separate IP budget)

| Endpoint | Use |
|----------|-----|
| `openInterestHist` | OI trend, divergence |
| `topLongShortAccountRatio` | Top trader accounts |
| `topLongShortPositionRatio` | Top positions |
| `globalLongShortAccountRatio` | `ls_ratio_extreme` |
| `takerlongshortRatio` | `aggression_shift` |
| `basis` | Basis % context |

**Scheduler rule:** 50 symbols × 5 endpoints = 250 calls — fit in 1000/5min only if batch interval ≥ 5–15 min for full shortlist.

## WebSocket routes

| Route | Streams |
|-------|---------|
| `/public` | `@bookTicker`, `!bookTicker`, `@depth`, `@depth@500ms` |
| `/market` | `@aggTrade`, `@markPrice`, `@kline_<interval>`, `@ticker`, `!ticker@arr`, `!markPrice@arr`, `!forceOrder@arr`, … |

## Typical stream budget (N=50 balanced)

| Component | Streams |
|-----------|---------|
| Global | ~3 (`!ticker@arr`, `!markPrice@arr`, `!forceOrder@arr`) |
| Book | 1 (`!bookTicker`) |
| Depth L2 | ~20 (`@depth20@500ms` top symbols) |
| Klines | N × \|trigger_tf union\| (often 50×15m = 50) |
| **Total** | ~74–124 | well under 1024 |

## Strategy → data mapping

| Data | Strategies (examples) |
|------|------------------------|
| klines 5m/15m/1h/4h | All TA/SMC |
| funding / premiumIndex | funding_reversal |
| openInterest + hist | oi_divergence, liquidation_heatmap |
| globalLongShortAccountRatio | ls_ratio_extreme |
| aggTrade | cvd_divergence, absorption, aggression_shift |
| depth / bookTicker | depth_imbalance, whale_walls, spread_strategy |
| forceOrder | liquidation_heatmap |
| multi ticker | altcoin_season_index, btc_correlation |

## Backlog (optional public)

- Spot `api.binance.com` for BTC dominance / basis context only.
- [WS Market Data API](https://github.com/openxapi/openxapi) for snapshot on reconnect (reduce REST storm).

## Coverage checklist (live)

Run: `PYTEST_LIVE=1 pytest tests/live/test_binance_public_api.py -v`

Extend live tests per family: funding, OI hist, global L/S, depth snapshot, aggTrade WS buffer.
