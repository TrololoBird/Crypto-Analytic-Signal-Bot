# Hunt market plane — CCXT + CCXT Pro

Hunt uses **only public** Binance USD-M endpoints. All market I/O goes through [CCXT](https://docs.ccxt.com/) (REST) and CCXT Pro (`watch*` WebSockets, merged into CCXT 4.x).

## Canonical exchange id

| Plane | Class | Config |
|-------|-------|--------|
| Futures REST | `ccxt.async_support.binance` | `options.defaultType: "future"` |
| Futures WS (Pro) | `ccxt.pro.binance` | same |
| Spot companion | `ccxt.async_support.binance` | `options.defaultType: "spot"` |
| Offline scripts | `ccxt.binance` (sync) | `defaultType: "future"` |

**Do not use `binanceusdm` for new code.** CCXT documents `binance` + `defaultType: future` as the unified USD-M path (same pattern as Freqtrade). `binanceusdm` exposes almost no `watch*` methods on Pro.

Factory: `hunt_core/market/ccxt_factory.py`.

## Lifecycle

```python
from hunt_core.market.factory import create_hunt_market_plane

plane = await create_hunt_market_plane()
# plane.client  — REST (OHLCV, OI, funding, fapiData*)
# plane.streams — Pro watch* (liquidations, trades, OHLCV, mark, books)
# plane.spot    — spot lead-lag

await plane.streams.start()
# ...
await plane.streams.stop()
await plane.client.close()   # closes REST + shared Pro instance
await plane.spot.close()
```

Rules:

- `enableRateLimit: True` always.
- `close_exchange_async` / `close_exchange_sync` in `ccxt_factory.py` — **never** `contextlib.suppress` on close; log `exchange_close_failed` on error.
- Check `exchange.has["watchMethod"]` before subscribing.
- On WS stall: cancel tasks, `close()` Pro, recreate via `acquire_pro_exchange()`.
- REST/WS fetch failures: `LOG.warning` then **re-raise** (or return `None` only when the API succeeded but field is absent). No stale-cache fallback after a failed refresh.
- Symbol mapping: **only** `exchange.market()` via `to_ccxt_symbol` / `from_ccxt_symbol` after `load_markets()`. No `BTC→BTC/USDT:USDT` heuristics.
- Timeframes: **only** `exchange.parse_timeframe()` — no static interval map fallback.

## Modules

| Module | Role |
|--------|------|
| `ccxt_factory.py` | Shared config + `create_*_binance_*` |
| `client.py` | `HuntCcxtClient` — REST + lazy Pro via `acquire_pro_exchange()` |
| `streams.py` | `HuntCcxtStreams` — multiplexed `watch*` loops |
| `ccxt_klines.py` | `fetch_klines_sync` / `fetch_klines_async` for scripts |
| `spot.py` | Spot prices vs futures mid |

## Environment

| Variable | Default | Effect |
|----------|---------|--------|
| `HTTP_PROXY` / `HTTPS_PROXY` | — | Passed via `trust_env` + explicit `proxy_url` in settings |
| `HUNT_MULTI_EXCHANGE` | on | `0` disables cross-venue intel (Bybit/OKX/Bitget) |
| `HUNT_CROSS_WS` | on | `0` disables Pro funding WS on secondaries |
| `HUNT_CROSS_REFRESH_S` | 300 | REST cross snapshot refresh interval |
| `HUNT_CROSS_MAX_SYMBOLS` | 24 | Max Binance watchlist symbols for cross refresh |

## Capability matrix (Binance USD-M via `ccxt.pro.binance`)

Verified watch methods include: `watchOHLCVForSymbols`, `watchTradesForSymbols`, `watchOrderBookForSymbols`, `watchLiquidationsForSymbols`, `watchMarkPrices`, `watchTickers`, `watchBidsAsks`, `watchFundingRates`.

Streams degrade gracefully when a method is missing (`exchange.has` gating).

## Scripts migrated off raw FAPI

- `param_calibration.py`, `reconcile_signals.py`, `tg_backtest.py` → `fetch_klines_sync`
- `backtest_signals.py`, `gate_edge.py` → `HuntCcxtClient` + `fetch_klines_async`

Telegram (`aiohttp`) and intel provider (`aiohttp`) are **not** Binance market data — kept as-is.

## References

- [CCXT Binance spot and futures wiki](https://github.com/ccxt/ccxt/wiki/binance-spot-and-futures)
- [CCXT Pro Python examples](https://github.com/ccxt/ccxt/tree/master/examples/ccxt.pro/py)
- [CCXT `close()` requirement](https://docs.ccxt.com/#/README?id=python)
