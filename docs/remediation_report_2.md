# Audit Report 2 Remediation Ledger

Date: 2026-05-09

Input reviewed: `audit_report (2).md`.

This document is not a replacement for code verification. It records what was
confirmed against the current repository, what was changed in this pass, and
what was rejected or deferred because the audit claim was stale, ambiguous, or
requires a wider design decision.

## Verification Sources

- Current local code and tests in this repository.
- Binance official WebSocket docs, checked 2026-05-09:
  - Spot stream limits: 5 incoming messages/s and 1024 streams/connection.
  - USD-M Futures stream limits: 10 incoming messages/s and 1024 streams/connection.
- PyPI pages, checked 2026-05-09:
  - `numpy` has a published `2.4.4` release.
  - `pytest-asyncio` has a published `1.3.0` release.

## Code Changes In This Pass

- WebSocket session shutdown now explicitly closes the socket, exits the loop,
  clears endpoint state, and treats normal close codes as graceful.
- WebSocket intended stream counts now enforce a local 300-stream safety limit.
- Public/bookTicker silence health checks use a tighter 15 second limit while
  keeping the generic configured limit for market/kline streams.
- Futures-data sliding-window limiter no longer sleeps while holding its lock.
- REST HTTP session now uses a shared `aiohttp.TCPConnector(limit=5)` matching
  the global REST semaphore.
- Aggregate-trade REST windows cap future `endTime` at current time.
- Stochastic now includes the current bar in the rolling high/low window.
- Supertrend branch selection was rewritten from chained ternaries to explicit
  `if`/`else` logic with equivalent behavior.
- Microstructure signed order flow clips `delta_ratio` before transforming.
- Strategy exceptions are converted into `SignalResult.error` instead of
  escaping the setup calculation boundary.
- Telegram sender rejects an empty token before constructing an aiogram bot.
- Shortlist premium-index merge preserves existing funding/basis values and
  only fills missing fields.
- EMA bounce now rejects missing context columns explicitly.
- Structure pullback now rejects shorts with RSI below 55 and reuses swing
  masks instead of recomputing them in both branches.
- Whale wall proxy rejects now distinguish conflicting depth/microprice from
  weak confirmation.
- Volume quality scoring uses a softer scale where normal volume is not
  penalized as severely.
- Signal-level `orderflow_delta_ratio` is now used by OI/orderflow scoring.
- Universe routing defensively handles missing liquidity rank, passes
  `shortlist_limit` into asset-fit context, and applies the volume floor to
  pinned symbols.
- Asset-fit `HIGH_VOLUME` rank tagging now respects configured
  `shortlist_limit` when settings are available.
- ADX penalty order is documented at the min-score gate.

## Audit Item Status

| ID | Status | Resolution |
| --- | --- | --- |
| 1 | Already fixed / false in current code | `handle_book_ticker` uses the supplied symbol as-is; manager symbol lists are normalized elsewhere. |
| 2 | Already fixed / false in current code | `_agg_trades` deque creation already occurs inside `manager._data_lock`. |
| 3 | Fixed | `run_stream_session` no longer returns from inside the `async with ws` block on shutdown. |
| 4 | Fixed with local guard | Runtime enforces a 300-stream safety limit per endpoint, below Binance's documented 1024 maximum. |
| 5 | Documented | Existing order was correct; added a comment explaining ADX penalty before min-score gating. |
| 6 | Already fixed / false in current code | `Signal.stop_distance_pct` exists as a property. |
| 7 | Fixed | EMA bounce now checks required columns before indexed access. |
| 8 | Fixed | Short structure-pullback RSI lower bound changed from 15 to 55. |
| 9 | Ambiguous / not changed | Existing FVG gate already combines relative bps width with ATR width. Audit math for tiny-price assets was not confirmed. |
| 10 | Fixed | Whale-wall reject reasons now distinguish conflict vs weak confirmation. |
| 11 | Fixed | `_volume_quality` now uses `min(max(ratio, 0.5) / 1.5, 1.0)`. |
| 12 | Fixed | Existing shared session now also uses `TCPConnector(limit=5)`. |
| 13 | Already fixed / false in current code | `_oi_participation_score` already checks `quote_volume > 0`. |
| 14 | Hardened | `_strategy_fits_for_row` now accepts `liquidity_rank: int | None`. |
| 15 | Deferred with reason | Realized volatility scale is a feature-contract decision. Changing it can invalidate trained models and historical thresholds. Needs a separate feature-version migration. |
| 16 | Already fixed / false in current code | `EventBus._safe_call` exists. |
| 17 | Already fixed / false in current code | `_ALL_SETUP_IDS` already includes the roadmap/phase strategies listed by the audit. |
| 18 | Fixed for maintainability | Supertrend branch logic is now explicit; parity tests still pass. |
| 19 | Fixed | Normal close codes are handled as graceful WebSocket closes. |
| 20 | Fixed | Public/bookTicker silence timeout is endpoint-specific and tighter than kline/market silence. |
| 21 | Already fixed / false in current code | REST URL validation checks parsed path separately from query string. |
| 22 | Fixed | `_SlidingWindowRateLimiter.acquire` releases its lock before sleeping. |
| 23 | Deferred | Wick-trap O(n) loop is small and not currently a measured bottleneck. Needs profiling before vectorization. |
| 24 | Fixed | Structure-pullback swing masks are computed once and reused. |
| 25 | Deferred | `_FrameCache` is synchronous by design; replacing with `asyncio.Lock` requires changing call contracts. |
| 26 | Fixed | Stochastic rolling high/low includes the current bar. |
| 27 | Fixed | Telegram sender validates non-empty bot token before aiogram construction. |
| 28 | Already fixed / low impact | `MLFilter` only loads the model when `enabled and use_ml_in_live` is true. |
| 29 | Already fixed / false in current code | `ScoringConfig` normalizes weights in a model validator. |
| 30 | Fixed | `delta_ratio` is clipped before signed order-flow transform. |
| 31 | Fixed | Premium-index merge fills missing fields instead of overwriting existing values. |
| 32 | Already fixed / false in current code | `is_deep_analysis_symbol` reads per-asset config. |
| 33 | Fixed | Base setup calculation catches strategy exceptions and returns an error decision. |
| 34 | Fixed | Asset-fit high-volume rank cutoff uses configured shortlist size when settings are provided. |
| 35 | Deferred | `_frame_is_fresh` object allocation is not a confirmed hot-path problem. |
| 36 | Stale external claim | PyPI currently lists `numpy 2.4.4`; dependency was not downgraded. |
| 37 | Low priority / not changed | `get_ws_url_version` is harmless unused helper surface. |
| 38 | Deferred | MiniTicker warm-up behavior needs live telemetry before changing startup load. |
| 39 | Already fixed / false in current code | `_min_ticker_update_interval_ms` is initialized in `FuturesWSManager`. |
| 40 | Already fixed / false in current code | Endpoint weights are used by `_estimate_weight` and pre-flight weight guard. |
| 41 | Deferred | EMA tolerance config cleanup is a compatibility/config migration task. |
| 42 | Deferred | Structure-pullback 1h fallback behavior changes signal semantics and needs strategy validation. |
| 43 | Low priority / not changed | Naming issue only; no confirmed behavior bug. |
| 44 | Deferred | Supertrend direction null handling needs targeted strategy fixtures before changing. |
| 45 | Already fixed / false in current code | Funding contrarian scoring uses `prepared.funding_rate`. |
| 46 | Deferred | VWAP session vs rolling VWAP is a strategy feature-contract decision. |
| 47 | Ambiguous / not changed | Audit says 30 minutes is too strict but recommends 20-25 minutes, which is stricter. |
| 48 | Deferred | Bucket priority rescale changes shortlist selection and needs replay comparison. |
| 49 | Already fixed / false in current code | Telegram queue already has `max_size=1000`. |
| 50 | Deferred | EventBus uses a short synchronous lock around in-memory deque operations; no measured blocking issue yet. |
| 51 | Documented risk | Registry tag AND semantics may be intentional; changing to OR is an API behavior change. |
| 52 | Already fixed / false in current code | Backtest max drawdown uses running max drawdown formula. |
| 53 | Already fixed / false in current code | No GPU-forcing code was found in ML train paths by grep. |
| 54 | Already fixed / false in current code | Core feature helpers call `ensure_columns` before required-column work. |
| 55 | Fixed | `Signal.orderflow_delta_ratio` participates in scoring via `_oi_momentum`. |
| 56 | Fixed | Aggregate-trade `endTime` is capped at current time. |
| 57 | Fixed | Pinned symbols must still meet the configured quote-volume floor. |
| 58 | Deferred | MessageBuffer counter cleanup is operational telemetry hygiene, not a confirmed leak. |
| 59 | Deferred | Broad import cleanup should be done with a dedicated ruff pass. |
| 60 | Deferred | `_as_float` deduplication is a refactor; no confirmed behavior issue. |
| 61 | Deferred | `get_optimizable_params` deduplication is a refactor; no confirmed behavior issue. |
| 62 | Deferred | Comment language cleanup is non-functional. |
| 63 | Deferred | Exception style cleanup is non-functional. |
| 64 | Deferred | Logging-style cleanup is non-functional. |
| 65 | Deferred | Auto-generating `__all__` changes import surface and needs separate review. |
| 66 | Deferred | Asset-fit profile specialization is strategy calibration work, not a safe blind patch. |
| 67 | Stale external claim | PyPI currently lists `pytest-asyncio 1.3.0`; dependency was not downgraded. |

## Follow-Up Tasks Created By This Review

These are not silent TODOs. They require separate design or replay validation
because changing them blindly can alter signal behavior or feature contracts.

1. Feature-contract migration: decide whether `realized_vol_20` should remain
   window-scaled, become intraday annualized, or be versioned as a new feature.
2. VWAP contract migration: compare current UTC-session VWAP against rolling
   VWAP on historical samples before changing strategy inputs.
3. Shortlist replay: evaluate `_bucket_priority` and pinned-symbol volume-floor
   impacts on historical/live shortlist snapshots.
4. Event-loop profiling: measure `_FrameCache`, EventBus locking, and
   WickTrap loops before async/vectorized rewrites.
5. Config cleanup: consolidate EMA tolerance aliases and repeated
   `get_optimizable_params` patterns with a migration path for existing config.
6. Broad style cleanup: run a dedicated ruff/import/logging pass separately from
   runtime logic changes.
