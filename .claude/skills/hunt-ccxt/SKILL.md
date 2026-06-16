---
name: hunt-ccxt
description: >-
  Mandatory CCXT context for Hunter (hunt/) market plane work. Read ccxt-python
  skill first, then hunt/docs/CCXT.md. Use when editing hunt/hunt_core/market/*,
  CCXT REST/Pro streams, Binance futures public data, or hunt scripts that fetch OHLCV.
---

# Hunter + CCXT

**Before any hunt market-plane edit**, load the official CCXT Python skill:

- Project: `.cursor/skills/ccxt-python/SKILL.md`
- Personal: `~/.cursor/skills/ccxt-python/SKILL.md`
- Invoke in chat: `/ccxt-python`

Then read project canon: `hunt/docs/CCXT.md`.

## Hunt constraints (non-negotiable)

- **Public endpoints only** — no API keys, no trading, no private methods
- Exchange: `ccxt.async_support.binance` / `ccxt.pro.binance` with `options.defaultType: "future"`
- **Do not use `binanceusdm`** for new code (Pro `watch*` coverage is poor)
- Always `enableRateLimit: True`
- Symbol mapping: `exchange.market()` via `to_ccxt_symbol` / `from_ccxt_symbol` after `load_markets()` — no string heuristics
- Timeframes: `exchange.parse_timeframe()` only
- Gate REST: `ccxt_method_available(ex, "fetchMethod")`
- Gate Pro WS: `ccxt_ws_method_available(ex, "watchMethod")` — requires `has[method] is True`
- CI gate: `python -m hunt_core._dev.check_ccxt` — blocks raw Binance HTTP / binanceusdm in hunt
- Lifecycle: always `close()` REST + Pro; log `exchange_close_failed`, never suppress close errors
- On fetch failure: log warning and re-raise on rate limits (no stale-cache fallback after failed refresh)
- Factory entry: `hunt_core/market/factory.py` → `create_hunt_market_plane()`
- **100% CCXT market plane** — no raw `fapi.binance.com` in `hunt/hunt_core/market/`; bootstrap via `fapipublicGetExchangeinfo` implicit API
- Field → CCXT debug map: `hunt_core.contract.MARKET_FIELD_CCXT_SOURCE`

## Funding / mark WS split (Binance primary)

- **Primary** mark/index/funding/basis: `watchMarkPrices` (`has=True`) → task `hunt_ccxt_mark`
- **Do not** start `watchFundingRates` on Binance — `has=None`, runtime `NotSupported`; `ccxt_plane_smoke` asserts `hunt_ccxt_funding` absent
- **Secondaries** (Bybit/OKX/Bitget): `watchFundingRates` when `HUNT_CROSS_WS=1` (default on via `load_cross_exchange_config()` + `apply_cross_exchange_env()` in watch loop)

## Key modules (13 files)

| Module | Role |
|--------|------|
| `factory.py` | Shared config + plane bootstrap + `fetch_klines_sync` |
| `client.py` | `HuntCcxtClient` — REST + lazy Pro |
| `streams.py` | `HuntCcxtStreams` — multiplexed `watch*` |
| `ccxt_rest.py` | `HuntCcxtRestGate` — weight pacing + 418/429 + `invoke`/`invoke_fapi` |
| `ccxt_guard.py` | `CcxtGuard`, `ccxt_method_available()`, `ccxt_ws_method_available()` |
| `network.py` | CCXT-first proxy probe (`filter_working_proxies_ccxt`) |
| `spot.py` | `HuntCcxtSpotCompanion` — gated spot ticker/OHLCV |
| `cross.py` | Secondary REST overlays + Pro funding WS mux |
| `symbols.py` | `exchange.market()` resolution |
| `live_price.py` | Live price from streams snapshot |
| `rate_limit.py` / `capacity.py` | Rate limit + per-tick load planner |

## REST call patterns

- Binance primary: `_rest_call` / `_direct_binance_fetch` / `_fapi_call` on `HuntCcxtClient`
- Allowed implicit API: `fapipublicGetExchangeinfo`, `fapiDataGet*` — see `CCXT.md` §Allowed implicit API
- Secondary venues: `_secondary_call` with `ccxt_method_available` + `record_error`
- Spot companion: `HuntCcxtRestGate.acquire_binance_weight` + `record_error`
- Pro config: `newUpdates: True`, `streaming.keepAlive` — see `build_network_config(pro=True)`
- Watch loop shares one `HuntCcxtClient` with scanner (`run_scan(client=...)`)

## CCXT references

- [CCXT Manual (REST)](https://github.com/ccxt/ccxt/wiki/Manual)
- [CCXT Pro Manual (WebSocket)](https://github.com/ccxt/ccxt/wiki/ccxt.pro.manual)
- [Binance spot & futures wiki](https://github.com/ccxt/ccxt/wiki/binance-spot-and-futures)
- [CCXT Pro Python examples](https://github.com/ccxt/ccxt/tree/master/examples/ccxt.pro/py)

## Error handling (from CCXT)

Retry with backoff: `NetworkError`, `RateLimitExceeded`, `DDoSProtection`, `RequestTimeout`.

Do not retry: `ExchangeError`, `AuthenticationError`, `InvalidOrder`.
