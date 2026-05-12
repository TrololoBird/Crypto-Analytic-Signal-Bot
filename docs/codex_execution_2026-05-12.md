# CODEX Audit Execution - 2026-05-12

## Scope

This report records the execution of `CODEX.md` audit/fix items against the
current local tree. Code and telemetry were treated as the source of truth;
stale audit recommendations were not applied blindly.

## Forensics

| Item | Status | Evidence |
| --- | --- | --- |
| `diag-paths-correlate` | confirmed | `data/bot/bot.pid` is absent. Latest matching log/run pair is `data/bot/logs/bot_20260511_222730_13756.log` and `data/bot/telemetry/runs/20260511_222730_13756/run_metadata.json`. Process `13756` is no longer running. |
| `diag-logs-grep` | confirmed | Latest log shows `ws_manager initialized`, `dashboard server started on port 8080`, `runtime initialized`, and shortlist refresh. No live dashboard process remains on `127.0.0.1:8080`. |
| `diag-jsonl-polars` | confirmed | `analysis/candidates.jsonl`: 4 rows. `selected.jsonl`: missing. `strategy_decisions.jsonl`: 42,180 rows, with `asset_fit.shortlist_not_routed` as top reason at 9,437 rows. `rejected.jsonl`: 42,176 rows; Polars schema inference hit mixed types, so JSON counters were used for that file after the Polars attempt. |
| `diag-sqlite-health` | confirmed | `data/bot/bot.db` exists. `active_signals`: 431 rows, all `closed`; `cooldowns`: 0; `signal_outcomes`: 395; `tracking_stats.signals_sent`: 431. Latest `health_runtime.jsonl` rows are `healthy` with `shortlist_size=45`. |
| `diag-dashboard-http` | confirmed | Log says dashboard started on port 8080, but `/api/status` and `/api/health` now refuse connection. `BOT_DISABLE_HTTP_SERVERS` is not set in the current shell. |

## Deep Audit Map

| Block | Inputs and contracts | Threshold/config source | Failure modes seen | Test references |
| --- | --- | --- | --- | --- |
| Strategies A/B | `STRATEGY_CLASSES` exports 37 classes; all inherit `BaseSetup`; all setup ids are present in `_ALL_SETUP_IDS` and enabled by default. Runtime contract is `detect(prepared, settings) -> Signal | None`. | Defaults come from each setup plus `settings.filters.setups[setup_id]` where provided. Current default `BotSettings` has no per-setup override map populated. | Strategy decisions show mostly `pattern.*`, `data.*`, and routing skips; no registry mismatch found. | `tests/test_strategies.py`, `tests/test_regression_suite_engine.py`, `tests/test_regression_suite_setups_contracts.py`. |
| Filters/features C | `prepare_symbol` enforces all configured TF minimums through `has_minimum_bars`; filters read primary timeframe via runtime policy and apply spread/ATR/RR/score/ADX gates. | `BotSettings.filters`, `runtime_policy`, per-setup overrides. | Confirmed silent ADX hard-gate downgrade for deep assets; now logged. `_FrameCache` used a blocking `RLock`; now non-blocking best-effort cache access. | `tests/test_features.py`, `tests/test_filters.py`, `tests/test_filters_adx_policy.py`. |
| Shortlist/universe D | `build_shortlist` computes `strategy_fits`; `SignalEngine` previously restricted shortlist execution by those fits; `rerank_shortlist` only changed volume/price fields. | `settings.universe`, `_ALL_SETUP_IDS`, `calculate_strategy_fit_score`. | Confirmed top telemetry reason `asset_fit.shortlist_not_routed`. Engine now runs all enabled strategies for shortlist assets. Rerank now recomputes bucket, score, liquidity rank, reasons, and fits. | `tests/test_regression_suite_engine.py`, `tests/test_universe_quote_asset.py`. |
| Binance I/O E | Runtime boundary remains public USD-M REST/WS only. URL/param validation rejects private/auth surfaces such as `listenKey`, `signature`, `apiKey`, and `timestamp`. WS split preserves `public` and `market` route classes. | `_PUBLIC_ENDPOINT_REGISTRY`, `_ENDPOINT_WEIGHTS`, `WSConfig`, runtime limiter settings. | `_call_rest` and `_call_public_http_json` duplicated circuit/rate-limit/validation guards; shared guard now used by both. `rest_full` vs `ws_light` telemetry remains explicit. | `tests/test_market_data_limits.py`, `tests/test_regression_suite_tracking_delivery.py`. |

## External Audit Reconciliation

| ID | Status | Result |
| --- | --- | --- |
| 1 | confirmed and fixed | `_FrameCache` no longer uses blocking `threading.RLock`; contended get/put skip cache instead of blocking the async analysis loop. |
| 2 | confirmed as installed | `python -m pip install -r requirements.txt --dry-run` completed with all requirements already satisfied on Python 3.13. |
| 3 | already-fixed/false | Polars EMA `span` semantics remain correct; no code change. |
| 4 | confirmed and fixed | Shared public REST preflight/guard path now serves both SDK and aiohttp calls. |
| 5 | already-fixed/false | `entry_reference_price` division remains guarded by `mid > 0`. |
| 6 | already-fixed/false | Freshness uses configured primary timeframe. |
| 7 | confirmed and fixed | Deep-analysis ADX hard-gate downgrade now emits an info log. |
| 8 | confirmed and fixed | Runtime shortlist analysis no longer depends on narrow `strategy_fits`; rerank recomputes fits. |
| 9-10, 23-24 | confirmed/no code change | `roadmap.py` RSI, StopHunt/Wyckoff, BB squeeze, and OI logic were inspected; no concrete inversion or contract bug was confirmed. |
| 11 | already-fixed/false | `TCPConnector` limit is 50. |
| 12 | already-fixed/false | Runtime URL uses `/futures/data/takerLongShortRatio`; only a cosmetic docstring mismatch remains. |
| 13 | already-fixed/false | WS gap backfill is bounded by semaphore and inflight tracking. |
| 14 | deferred with reason | `asyncio.sleep(0)` hot-path impact requires profiling on a live `!ticker@arr` stream. No confirmed regression from current telemetry. |
| 15 | ambiguous | `_supertrend`, `_parabolic_sar`, and `_fisher_transform` intentionally carry iterative indicator state. Replacing them without parity proof would be riskier than the confirmed issue set. |
| 16-17 | deferred with reason | WS event age compares local epoch time to exchange epoch timestamps, so monotonic time cannot replace it directly without adding receipt-time telemetry. |
| 19 | already-fixed/false | `has_minimum_bars` checks all configured frames. |
| 20 | confirmed and fixed | `rerank_shortlist` now recalculates score, bucket, liquidity rank, reasons, and `strategy_fits`. |
| 30 | ambiguous | Realized volatility is a window-scaled feature, not documented as annualized. No runtime defect confirmed. |

## Code Changes

- Engine: shortlist assets run all enabled strategies, eliminating runtime dependence on `UniverseSymbol.strategy_fits` for live shortlist coverage.
- Universe: WebSocket rerank keeps score/fits current instead of carrying stale routing hints.
- Dashboard: `/api/status` includes `btc_bias`; UI formatters preserve zero values and refresh/hotkey code is null-safe.
- Telegram/delivery: startup calls delivery preflight non-fatally; non-`sent` delivery results are warning-level telemetry.
- Features: frame cache lock contention no longer blocks the async analysis loop.
- Market data: public REST validation, circuit, limiter, pause, and weight guard logic is shared.

## Deferred Follow-ups

- Recalibrate or disable poor-expectancy setups only after collecting post-routing telemetry; pre-fix outcome data is confounded by missing routing coverage.
- A single Telegram boundary and a full TF/freshness map are design-level follow-ups; this pass documented the runtime boundary and fixed the concrete delivery observability gap.

## Second Pass - Delivery, Dashboard, Scripts

| Item | Status | Evidence |
| --- | --- | --- |
| Telegram not sending | confirmed and fixed | Latest inspected run `data/bot/telemetry/runs/20260512_015133_2620` had candidates and delivery status `logged`, but no `selected.jsonl`/Telegram sends because notifier provider resolved to `none`. Provider now auto-promotes to `telegram` when both Telegram secrets are present, and disabled delivery preflight fails loudly. |
| Duplicate same-symbol signals | confirmed and fixed | Same-cycle same-symbol/same-direction confluence is represented in the rendered Telegram card as `Confluence N setups: ...`; telemetry now records selected attempts separately from delivered sends. |
| Dashboard recent signals | confirmed and fixed | Dashboard now reads `selected.jsonl`, then `delivery.jsonl`, then candidate fallback, so local/logged delivery attempts are visible instead of disappearing when Telegram is disabled. |
| Strategy audit | confirmed targeted fix | Contract suites passed for strategy exports. A confirmed roadmap bug was fixed: non-finite OI values no longer generate OI-divergence signals, and zero long/short ratio is treated as a valid extreme rather than missing. |
| Script inventory drift | confirmed and fixed | Deprecated thin wrappers were removed; `Makefile`, `run_30min_test.bat`, `docker-compose.yml`, `scripts/README.md`, and CLI help were aligned with the current live-only runtime. |
