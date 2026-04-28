# REST audit: rate-limits, retry/backoff, fail-fast, and degradation paths

Date: 2026-04-28
Scope: `bot/market_data.py` REST client + direct runtime consumers in `bot/application/*`, `bot/tracking.py`, `bot/public_intelligence.py`.

## Confirmed facts

### 1) Rate-limit handling (429/418 and related)

- Core handling is centralized in `BinanceFuturesMarketData`:
  - Reads `Retry-After` and converts it into global pause (`_set_rate_limit_pause`).
  - Tracks server weight header `x-mbx-used-weight-1m` and applies staged pauses at soft/hard/critical thresholds.
  - Applies pre-flight client-side weight guard before request dispatch.
  - Uses a separate sliding-window limiter for `/futures/data/*` IP-based quotas.
- HTTP 429:
  - Increments `rate_limit_error_streak`.
  - Uses `Retry-After` when present, else fallback exponential backoff.
  - Raises `MarketDataUnavailable` after setting pause.
- HTTP 418:
  - Treated as IP ban.
  - Enforces minimum 1800s pause (30m), regardless of `Retry-After`.
  - Logs CRITICAL and fails request with `MarketDataUnavailable`.

### 2) Retry/backoff strategy

- Generic REST path (`_call_public_http_json`, `_call_rest`):
  - No automatic per-request retry loop for failed calls; calls fail fast after applying pause/circuit accounting.
  - Backoff calculation exists (`_calculate_backoff`) and is used for 429 when `Retry-After` absent.
  - Backoff form: exponential `base_delay * 2^attempt`, jitter multiplier `uniform(0.5, 1.5)`, capped.
- Localized retries:
  - `fetch_book_ticker_rest`: retries up to 3 attempts only for timeout-like errors with backoff `0.5 * 2^(attempt-1)` plus jitter `0.9..1.1`, capped at 2s.
  - `ShortlistService.fetch_symbols_with_retry`: 1 retry (`max_retries=1` by default), fixed 1s sleep on timeout/exception.

### 3) Timeouts, circuit-breaker-like mechanisms, fail-fast

- Timeouts:
  - REST calls are wrapped by `asyncio.wait_for(..., timeout=self._rest_timeout)` in `_call_rest`.
  - aiohttp session uses `ClientTimeout(total=self._rest_timeout)`.
  - Runtime default timeout in config model is `10.0s`; current `config.toml` sets `12.0s`.
- Circuit breaker:
  - Per-operation breaker in `BinanceFuturesMarketData` with hardcoded params:
    - threshold: 3 failures,
    - open duration: 30s.
  - Open-circuit path returns `MarketDataUnavailable` immediately (fail-fast).
- Important discrepancy:
  - `RuntimeConfig` defines `circuit_breaker_failure_threshold` and `circuit_breaker_cooldown_seconds`, but REST client currently uses internal hardcoded 3/30 values.
- Fail-fast examples:
  - `_call_public_http_json` and `_call_rest` short-circuit when breaker open.
  - `OIRefreshRunner` skips symbol fetch when `open_interest_statistics` circuit is open.
  - `public_intelligence._build_derivatives_snapshot` does sequential awaits without local exception handling; first propagated error aborts snapshot construction.

### 4) Behavior when `OI`, `LS`, `funding`, `aggTrades` unavailable

- OI / LS / funding (REST enrichments):
  - `fetch_open_interest`, `fetch_open_interest_change`, `fetch_long_short_ratio`, `fetch_funding_rate`, `fetch_funding_rate_history`, `fetch_taker_ratio`, `fetch_global_ls_ratio`, `fetch_basis`:
    - prefer fresh cache by TTL,
    - on `MarketDataUnavailable`, return stale cache if present,
    - otherwise return `None` (or `[]` for funding history).
  - This is explicit quality degradation with cache-first fallback behavior.
- `aggTrades`:
  - Tracking review first tries aggTrades pagination.
  - On timeout/unavailable/errors -> falls back to 1m candles.
  - If candles also unavailable -> falls back to time-based expiry logic.
  - This is multi-stage degradation: `trade precision` -> `candle precision` -> `time_fallback`.

### 5) Silent degradation risk (fallback without explicit telemetry)

High-risk or notable places:

- `OIRefreshRunner._safe_fetch`: broad `except Exception: pass` per fetcher (OI/LS/funding warmups) with no logging/telemetry.
- `SymbolAnalyzer.ws_enrich`: wraps OI fetch sequence in broad `except Exception: pass`.
- `SymbolAnalyzer` WS context pulls (`ticker/mark/depth`) have several broad `except Exception: pass` blocks.
- `fetch_open_interest`, `fetch_open_interest_change`, `fetch_long_short_ratio` fallback-to-stale paths log only at `DEBUG`; in production this can become effectively silent.
- `fetch_funding_rate` stale fallback has no warning log at all (only endpoint snapshot state), so degradation can be hard to detect without state polling.

Lower-risk / partially instrumented:

- `fetch_ticker_24h` stale fallback logs warning (good).
- Tracking aggTrades->candles->time fallback logs debug/warning at each stage (good).
- Endpoint snapshot (`state_snapshot`) records `fallback_used`, but this is pull-based diagnostics; no guaranteed emitted event per degradation incident.

## Unverified inferences / caveats

- This audit is static (code-path review). No live Binance run was executed, so runtime frequency/impact of degradations is not measured.
- Presence of `state_snapshot().fallback_used` suggests observability intent, but no evidence in this pass that snapshots are continuously exported to alerting.
