# Binance Endpoint Registry Audit

_Date:_ 2026-04-28 (UTC)

## Scope

- Scanned `bot/` for Binance REST/WS base URLs, endpoint paths, and WS stream names.
- Focused on network-facing modules:
  - `bot/market_data.py`
  - `bot/ws_manager.py`
  - `bot/websocket/subscriptions.py`
  - `bot/websocket/connection.py`
  - `bot/config.py`
  - `bot/public_intelligence.py`

## Base URLs

| Protocol | Base URL | Source |
|---|---|---|
| REST (USDⓈ-M Futures) | `https://fapi.binance.com` | `bot/market_data.py::_FAPI_BASE_URL` |
| WS (Futures streams root) | `wss://fstream.binance.com` (normalized) | `bot/config.py::WSConfig` + `bot/websocket/connection.py::build_stream_url` |

> Effective WS connect URL is `<base>/stream`.

## REST endpoint registry

| Method/Path | Purpose | Signed/Public | API key required |
|---|---|---|---|
| `GET /fapi/v1/exchangeInfo` | Futures symbol metadata/universe | Public | No |
| `GET /fapi/v1/ticker/24hr` | 24h ticker stats | Public | No |
| `GET /fapi/v1/klines` | OHLCV candles | Public | No |
| `GET /fapi/v1/ticker/bookTicker` | Best bid/ask | Public | No |
| `GET /fapi/v1/aggTrades` | Aggregate trades | Public | No |
| `GET /fapi/v1/premiumIndex` | Mark/index + funding snapshot (`lastFundingRate`) | Public | No |
| `GET /fapi/v1/openInterest` | Current open interest | Public | No |
| `GET /fapi/v1/fundingRate` | Funding history | Public | No |
| `GET /futures/data/openInterestHist` | OI history/change calc | Public (IP-rate-limited) | No |
| `GET /futures/data/topLongShortAccountRatio` | Top trader account L/S | Public (IP-rate-limited) | No |
| `GET /futures/data/topLongShortPositionRatio` | Top trader position L/S | Public (IP-rate-limited) | No |
| `GET /futures/data/globalLongShortAccountRatio` | Global account L/S | Public (IP-rate-limited) | No |
| `GET /futures/data/takerlongshortRatio` | Taker buy/sell ratio | Public (IP-rate-limited) | No |
| `GET /futures/data/basis` | Basis (% contango/backwardation proxy) | Public (IP-rate-limited) | No |
## WS stream names

| Stream name pattern | Purpose | Signed/Public | API key required |
|---|---|---|---|
| `<symbol>@kline_<interval>` | Per-symbol klines (`5m/15m/1h` by config) | Public | No |
| `<symbol>@bookTicker` | Per-symbol top-of-book | Public | No |
| `<symbol>@aggTrade` | Per-symbol aggregate trades (tracked symbols) | Public | No |
| `!ticker@arr` | Global 24h ticker array | Public | No |
| `!markPrice@arr` | Global mark price updates | Public | No |
| `!forceOrder@arr` | Global liquidation feed | Public | No |
| `!miniTicker@arr` | Global lightweight ticker fallback | Public | No |

## Explicit guardrails found in code

- REST registry validator allows only public prefixes: `/fapi/v1/`, `/futures/data/`.
- Runtime REST host validator allows only `https://fapi.binance.com`; non-USDⓈ-M hosts like `eapi.binance.com` are rejected.
- Runtime options snapshot path in `bot/public_intelligence.py::_build_options_snapshot(...)` is hard-disabled for eAPI and returns unavailable metadata under the USDⓈ-M runtime boundary.
- REST registry validator forbids private/auth markers: `/private`, `listenkey`, `/ws-api`, `/sapi`, `/papi`, `signature=`, `timestamp=`.
- WS stream classifier rejects private/auth stream tokens: `listenkey`, `/private`, `userdatastream`, `@account`, `@order`.
- Runtime WS config validator forbids `/private`, `listenkey`, `/ws-api`, `/sapi`, `/papi` in configured WS URLs.

## Absence checks

### 1) Account/trade endpoints
- Result: **Not found** under `bot/` for Binance futures private routes such as `/fapi/v1/order`, `/fapi/v2/account`, `/fapi/v1/userTrades`, etc.

### 2) `listenKey`
- Result: **Not found as actual endpoint usage**.
- Found only in defensive validators/deny-lists (expected and compliant).

### 3) User-data streams
- Result: **Not found as subscribed streams**.
- Found only in WS deny-list guard (`userdatastream`, `@account`, `@order`) (expected and compliant).

### 4) Signed query
- Result: **Not found** (`signature=` / timestamp-based signed route usage absent in requests).
- Found only in forbidden-marker validation list (expected and compliant).

## Non-conformities / policy mismatches

| Severity | Finding | Module source |
|---|---|---|
| Informational | eAPI option helpers remain available only for explicit non-runtime research workflows; runtime path does not call them. | `bot/public_intelligence.py` |

## Compliance verdict

**Compliant**.

### Blocking items

1. None.

### Compliant items

- No account/trade private endpoints detected.
- No `listenKey` lifecycle usage detected.
- No user-data WS streams detected.
- No signed query usage detected.
- WS and REST validators include explicit deny-lists for private/auth surfaces.
