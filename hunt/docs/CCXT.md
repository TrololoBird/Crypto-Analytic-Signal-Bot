# Hunt market plane — CCXT + CCXT Pro (100%, enforced)

Hunt uses **only public** Binance USD-M endpoints. **All** market I/O goes through [CCXT](https://docs.ccxt.com/) (REST + implicit API) and CCXT Pro (`watch*` WebSockets, merged into CCXT 4.x).

**Status (2026-06-15):** Zero raw `fapi.binance.com` HTTP in `hunt/hunt_core/market/`. CI gate: `python -m hunt_core._dev.check_ccxt`.

## Canonical exchange id

| Plane | Class | Config |
|-------|-------|--------|
| Futures REST | `ccxt.async_support.binance` | `options.defaultType: "future"` |
| Futures WS (Pro) | `ccxt.pro.binance` | same + `pro=True` factory flags |
| Spot companion | `ccxt.async_support.binance` | `options.defaultType: "spot"` |
| Secondary venues | `ccxt.async_support.{bybit,okx,bitget}` | `defaultType: "swap"` |
| Offline scripts | `ccxt.binance` (sync) | `defaultType: "future"` |

**Do not use `binanceusdm` for new code.** CCXT documents `binance` + `defaultType: future` as the unified USD-M path. `binanceusdm` exposes almost no `watch*` methods on Pro.

Factory: `hunt_core/market/factory.py`.

## REST call patterns

| Path | Helper | Gate |
|------|--------|------|
| Binance fetch_* | `_rest_call` / `_direct_binance_fetch` | `ccxt_method_available` |
| Binance fapiData* | `_fapi_call` → `invoke_fapi` | `callable(fetcher)` |
| Binance bootstrap | `fapipublicGetExchangeinfo` implicit | after 3× `load_markets` retry |
| Secondary venues | `_secondary_call` → `invoke_secondary` | `ccxt_method_available` |
| Spot companion | `_spot_fetch` | `HuntCcxtRestGate` + has check |
| Cross overlays | `client.rest_gate.invoke_secondary` | `ccxt_method_available` |

All gated paths record 418/429 via `HuntCcxtRestGate.record_error`.

## Allowed CCXT implicit API (not raw HTTP)

These are **CCXT unified implicit methods** on `ccxt.async_support.binance` — allowed in `hunt_core/market/`:

| Implicit method | Hunt use |
|---------------|----------|
| `fapipublicGetExchangeinfo` | Market bootstrap fallback |
| `fapiDataGetTopLongShortAccountRatio` | L/S account ratio |
| `fapiDataGetTopLongShortPositionRatio` | Top position L/S |
| `fapiDataGetGlobalLongShortAccountRatio` | Global L/S |
| `fapiDataGetTakerlongshortRatio` | Taker buy/sell ratio |
| `fapiDataGetBasis` | Basis % (fallback: mark/index OHLCV) |

## Pro exchange config

When `pro=True` in `build_network_config`:

| Key | Value | Why |
|-----|-------|-----|
| `newUpdates` | `True` | Delta-only WS updates (CCXT Pro manual) |
| `streaming.keepAlive` | `30000` | WS ping interval |
| `streaming.maxPingPongMisses` | `2.0` | Reconnect after missed pongs |
| `options.tradesLimit` | `500` | In-memory trade buffer cap |
| `options.OHLCVLimit` | `200` | In-memory OHLCV buffer cap |
| `options.watchOrderBookLimit` | `20` | Depth snapshot weight cap (anti-418) |

## WebSocket gating

| Gate | Use |
|------|-----|
| `ccxt_method_available` | REST `fetch*` and secondary venues |
| `ccxt_ws_method_available` | Pro `watch*` — requires `has[method] is True` |

**Binance funding/mark/index:** primary source is `watchMarkPrices` (`has=True`). `watchFundingRates` has `has=None` and raises `NotSupported` at runtime — not started.

## Proxy discovery

Proxy probing uses **CCXT `load_markets()`** exclusively:

- `filter_working_proxies()` → `filter_working_proxies_ccxt()`
- `auto_discover_proxies()` → `_probe_ccxt_markets()` per candidate
- `probe_ccxt_direct()` — startup direct-access check

`aiohttp` in `network.py` is **only** for fetching public proxy list URLs (not Binance market data).

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
- `close_exchange_async` / `close_exchange_sync` in `factory.py` — **never** `contextlib.suppress` on close; log `exchange_close_failed` on error.
- REST/WS fetch failures: `LOG.warning` then **re-raise** on rate limits; documented soft fallbacks only (e.g. basis → mark/index OHLCV).
- Symbol mapping: **only** `exchange.market()` via `to_ccxt_symbol` / `from_ccxt_symbol` after `load_markets()`.
- Timeframes: **only** `exchange.parse_timeframe()` — no static interval map fallback.

## Modules

| Module | Role |
|--------|------|
| `factory.py` | Shared config, plane bootstrap, `fetch_klines_sync` / `fetch_klines_async` |
| `client.py` | `HuntCcxtClient` — REST + lazy Pro via `acquire_pro_exchange()` |
| `streams.py` | `HuntCcxtStreams` — multiplexed `watch*` loops |
| `ccxt_rest.py` | `HuntCcxtRestGate` — `invoke`, `invoke_fapi`, `invoke_secondary` |
| `ccxt_guard.py` | `CcxtGuard`, `ccxt_method_available()`, `ccxt_ws_method_available()` |
| `network.py` | CCXT proxy probe + `ProxyPool` |
| `rate_limit.py` / `capacity.py` | Sliding windows + tick load planner |
| `spot.py` | Spot prices vs futures mid (CCXT spot) |
| `cross.py` | Multi-venue REST overlays + secondary Pro funding WS |
| `symbols.py` | Symbol resolution via `exchange.market()` |
| `live_price.py` | Live price from `HuntCcxtStreams` snapshot |
| `capacity.py` | Per-tick REST load planner (`HuntLoadPlanner`) |

Public exports: `from hunt_core.market import ccxt_method_available, ccxt_ws_method_available, HuntCcxtRestGate, probe_ccxt_direct`.

Field → CCXT source map for readiness/debug: `hunt_core.contract.MARKET_FIELD_CCXT_SOURCE`.

## CCXT → Binance endpoint map

| Hunt method | CCXT surface |
|-------------|--------------|
| `fetch_klines` | `fetchOHLCV` |
| `fetch_ticker_24h` | `fetchTickers` / `fetchTicker` |
| `load_markets` / universe | `loadMarkets` or `fapipublicGetExchangeinfo` fallback |
| `fetch_open_interest` | `fetchOpenInterest` |
| `fetch_funding_rate*` | `fetchFundingRate` / `fetchFundingRateHistory` |
| `fetch_premium_index_all` | `fetchFundingRates` |
| `fetch_funding_info_all` | `fetchFundingIntervals` |
| `fetch_taker_ratio`, L/S ratios | `fapiDataGet*` implicit |
| `fetch_basis` | `fapiDataGetBasis` (fallback: mark/index OHLCV) |
| `fetch_mark_ohlcv` | `fetchMarkOHLCV` |
| `fetch_order_book_depth_snapshot` | `fetchOrderBook` |
| `fetch_agg_trade_snapshot` | `fetchTrades` |
| Pro streams | `watchOHLCVForSymbols`, `watchTradesForSymbols`, `watchMarkPrices`, … |

## Environment

| Variable | Default | Effect |
|----------|---------|--------|
| `HTTP_PROXY` / `HTTPS_PROXY` | — | Passed via `trust_env` + explicit `proxy_url` in settings |
| `HUNT_MULTI_EXCHANGE` | on | `0` disables cross-venue intel (Bybit/OKX/Bitget) |
| `HUNT_CROSS_WS` | on | Default **on** via `load_cross_exchange_config()` + `apply_cross_exchange_env()` in watch loop; `0` disables Pro `watchFundingRates` on secondaries |
| `HUNT_CROSS_REFRESH_S` | 300 | REST cross snapshot refresh interval |
| `HUNT_CROSS_MAX_SYMBOLS` | 24 | Max Binance watchlist symbols for cross refresh |

## Capability matrix (Binance USD-M via `ccxt.pro.binance`)

| CCXT Pro method | `has` | Hunt task | Notes |
|-----------------|-------|-----------|-------|
| `watchLiquidationsForSymbols` | True | `hunt_ccxt_liq` | Primary liquidation cascades |
| `watchLiquidations` / `watchLiquidationsForSymbols` | Bybit/OKX | `hunt_ccxt_liq_cross` | Multi-exchange real liq when `HUNT_CROSS_WS=1` + `HUNT_MAPS_LIQ_CROSS=1` |
| `watchMarkPrices` | True | `hunt_ccxt_mark` | **Primary** mark/index/funding/basis |
| `watchTradesForSymbols` | True | `hunt_ccxt_trades` | Agg trade delta |
| `watchOHLCVForSymbols` | True | `hunt_ccxt_kline` / `_5m` / `_15m` | Closed-bar kline WS |
| `watchOrderBookForSymbols` | True | `hunt_ccxt_book` | Depth imbalance |
| `watchTickers` | True | `hunt_ccxt_tickers` | 24h ticker enrich |
| `watchBidsAsks` | True | `hunt_ccxt_bbo` | Top-of-book |
| `watchFundingRates` | None | — | **Not started** on Binance (runtime `NotSupported`) |
| `watchFundingRates` | OKX `True` | `hunt_ccxt_funding_cross` | Live WS funding on OKX when `HUNT_CROSS_WS=1` |
| `watchFundingRates` | Bybit/Bitget `None` | `hunt_ccxt_funding_rest` | REST poll (`HUNT_CROSS_FUNDING_REST_S`, default 60s) — not JSON null |
| `watchOHLCV` / `watchTrades` / `watchOrderBook` | True | — | Superseded by `*ForSymbols` multiplex |
| `watchBalance`, `watchOrders`, `watchPositions`, `watchMyTrades` | True | — | **Private/auth — forbidden** (public-only bot) |

`ccxt_plane_smoke` asserts `hunt_ccxt_funding` is absent on Binance primary.

Streams degrade gracefully when `ccxt_ws_method_available` is false.

## CI verification

```bash
# repo root, after pip install -e "./hunt"
python -m hunt_core._dev.check_ccxt
python -m hunt_core._dev.ccxt_plane_smoke BTCUSDT ETHUSDT --ws-seconds 3
python -m hunt_core._dev.check_logic
```

Offline analysis and backtests use `hunt_core._dev.*` and `hunt_core.market.factory.fetch_klines_sync` — no `hunt/scripts/` tree.

### Implicit API health (fapiDataGet*)

Unified methods (`fetchOHLCV`, `fetchOpenInterest`) are stable; **implicit** `fapiDataGet*` wrappers break first on CCXT upgrades. CI gate: `python -m hunt_core._dev.check_ccxt` (method availability + no raw HTTP). After CCXT bump, re-run `ccxt_plane_smoke` and diff `ccxt_method_available()` for OI/taker/basis pack.

## Liquidation score convention (T1 / A1)

| Field | Range | Meaning |
|-------|-------|---------|
| `liquidation_score` | `[0, 1]` | Normalized cascade pressure from WS liq + OI context |
| `ws_liq_cascade_score` | `[0, 1]` | Raw WS liquidation cascade component |

**Threshold:** treat `liquidation_score <= 0.30` as **no meaningful liq fuel** — do not surface in user-facing pros or structural hard counts.

**Read sites:** `contract.parse_liquidation_score`, `detect/fusion.py`, `gate/delivery.py` (`_structural_hard_count` excludes bare `ws_liq` key), `detect/phase.py`.

## Multi-venue liq → maps forward overlay + forecast (mission)

Real liquidation events from Binance primary + Bybit/OKX cross WS (`hunt_ccxt_liq` / `hunt_ccxt_liq_cross`, gated by `HUNT_CROSS_WS=1` + `HUNT_MAPS_LIQ_CROSS=1`) land in `MapTimeSeriesStore` per-venue buffers (`market/streams.py:_record_liquidation`). `maps/liquidation.build_liquidation_map` projects entry-anchored **forward** liq zones and blends them with **realized** zones; forward confidence resolves via `_resolved_forward_confidence` → persisted `calibrated_forward_confidence` (from `_dev.maps_calibration_probe --persist`) → `0.25 + event_count*0.04` → config `forward_blend_ratio`.

`liq_heatmap_nearest_short` (nearest short-squeeze magnet above price) is a primary **pump target** input to `maps/forecast.build_maps_forecast` (`kind="prepump_long"`), surfaced on `/signal`, `/analyze` pinned, and confirm/ARMED cards. The calibration loop closes via `gate/_phase_matrix.phase_matrix_gate` (advisory blocker `phase_matrix_disable`, EV-primary bypass).

**Schema:** `hunt_core/domain/schemas.py` documents `[0,1]` for signal contract fields.

## Non-market aiohttp (intentional)

Telegram delivery, intel HTTP providers, and public proxy-list downloads use `aiohttp` — **not** Binance market data.

## References

- [CCXT Manual (REST)](https://github.com/ccxt/ccxt/wiki/Manual)
- [CCXT Pro Manual (WebSocket)](https://github.com/ccxt/ccxt/wiki/ccxt.pro.manual)
- [CCXT Binance spot and futures wiki](https://github.com/ccxt/ccxt/wiki/binance-spot-and-futures)
- [CCXT Pro Python examples](https://github.com/ccxt/ccxt/tree/master/examples/ccxt.pro/py)
