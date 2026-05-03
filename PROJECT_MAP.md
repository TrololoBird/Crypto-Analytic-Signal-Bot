# Project Map and Audit Notes

Date: 2026-05-03

This map is based on the current repository state, targeted source reads, local
import checks, telemetry JSONL parsing, and official dependency/API references.
It distinguishes implemented/reachable behavior from unverified trading validity.

## Reflection

Tentative answer: the project has 21 strategy classes, an SMC helper layer, a
public Binance USD-M market-data boundary, and an event-driven Telegram signal
pipeline.

Self-critique: a class export does not prove market edge, live validity, or full
SMC parity. Documentation can also be stale. I therefore verified the call path
against source, counted the exported strategies at runtime, checked config keys,
read strategy tests, parsed telemetry for the BOS/CHoCH anomaly, and checked the
Binance WS and package metadata externally.

Revised conclusion: the 21 strategies are implemented and reachable by the
runtime registry. Their profitability and live-market correctness are not proven
by this audit.

## Confirmed Runtime Map

- Entry point: `main.py` calls `bot.cli.run()`.
- CLI path: `bot.cli._main()` loads `config.toml`, validates settings, creates
  `bot.application.bot.SignalBot`, and starts it.
- Orchestration: `bot/application/bot.py` wires market data, WebSocket streams,
  shortlist management, Telegram delivery, telemetry, tracking, and the modern
  strategy engine.
- Strategy registration: `SignalBot._register_strategies_to_registry()` imports
  `STRATEGY_CLASSES`, reads `[bot.setups]`, instantiates enabled strategies, and
  registers them in `StrategyRegistry`.
- Engine contract: `bot/core/engine/engine.py` runs enabled strategies against
  `PreparedSymbol` objects and records `StrategyDecision` telemetry.
- Analysis pipeline: `bot/application/symbol_analyzer.py` prepares features,
  runs the engine, applies family precheck, alignment penalties, family
  confirmation, performance guards, global filters, and final scoring.
- Runtime triggers: `bot/application/kline_handler.py` handles 15m kline-close
  events; `bot/application/intra_candle_scanner.py` handles intrabar scans;
  fallback cycles run through `bot/application/fallback_runner.py`.
- Delivery: selected signals flow through `bot/application/delivery_orchestrator.py`
  and Telegram infrastructure under `bot/telegram/`.
- Persistence: runtime memory and tracking go through `bot/core/memory/`.

## Public API and WS Boundary

Confirmed from code:

- REST boundary validation is in `bot/market_data.py` and `bot/config.py`.
- WS config defaults use Binance USD-M routed endpoints:
  `wss://fstream.binance.com/public` and `wss://fstream.binance.com/market`.
- `bot/websocket/subscriptions.py` rejects private/auth stream markers such as
  `listenKey`, `/private`, user-data, account, and order streams.
- The code preserves the public vs market stream split:
  book ticker/depth -> `public`; kline, aggTrade, markPrice, ticker arrays,
  liquidation arrays -> `market`.

Confirmed from official Binance docs on 2026-05-03:

- USD-M WS base URL is `wss://fstream.binance.com`.
- Current docs describe routed endpoints `/public`, `/market`, and `/private`.
- `/ws/<streamName>` and `/stream?streams=...` access modes are supported.
- Combined stream payloads are wrapped as `{"stream": "...", "data": ...}`.
- Connections are valid for 24 hours.
- Server ping is every 3 minutes; missing pong for 10 minutes disconnects.
- Limit is 10 incoming messages per second and 1024 streams per connection.

Local fit:

- `bot/websocket/connection.py` builds `<routed endpoint>/stream`.
- It uses `websockets.connect(..., ping_interval=20.0, ping_timeout=20.0,
  close_timeout=10.0)`, and `websockets` 16.0 documents these parameters.
- `bot/ws_manager.py` proactively reconnects before 24h and keeps a conservative
  stream/message limit below the Binance hard limit.
- `WSConfig.subscribe_chunk_delay_ms` now validates at `>=100ms`, matching the
  10 incoming control messages/sec Binance limit.
- `/futures/data/*` REST calls now use a configurable shared-IP safety budget:
  `runtime.futures_data_request_limit_per_5m`, default `300`, clamped to the
  official `1000/5m` ceiling.

## Dependency Map

Confirmed local state after requirements consolidation:

- Only `requirements.txt` remains among `requirements*.txt` files.
- It now includes runtime, dev/test, live-check/dashboard, regime, and ML extras.
- Resolver check passed with Python 3.13.13:
  `python -m pip install -r requirements.txt --dry-run`.
- Binary wheel resolver check also passed:
  `python -m pip install --dry-run --only-binary=:all: -r requirements.txt`.
- `polars_ta` 0.5.17 metadata lists `Provides-Extra: talib`, not a mandatory
  TA-Lib dependency.
- Local import-hook test blocked `talib` and still executed `polars_ta.ta.SMA`
  and `bot.features._ema`, confirming the project can keep `polars_ta`
  installed without requiring native TA-Lib.
- `bot.features` now keeps runtime indicator generation on the deterministic
  pure-Polars path (`_HAS_TALIB=False`, `_USE_POLARS_TA_BACKEND=False`) because
  the installed `polars_ta` backend changed warm-up semantics and lacked at
  least one grouped-module TA function (`ADX`) in local tests.
- `websockets>=16.0,<17.0` is compatible with Python 3.13 per PyPI metadata and
  exposes the client parameters used by the code.

Residual uncertainty:

- Package metadata and dry-run resolution prove installability for the current
  interpreter/platform, not application correctness under all live conditions.
- Some packages with native wheels may still fail on unusual Windows toolchains
  if a future version removes wheels; the current binary dry-run did not show
  that problem.

## Strategy Inventory

Confirmed facts:

- `bot/strategies/__init__.py` exports 21 concrete strategy classes.
- Runtime import check returned 21 classes after the phase 5.3 expansion.
- All 21 have `[bot.setups]` enable flags in `config.toml` and
  `config.toml.example`.
- All 21 have `[bot.filters.setups]` parameter blocks in `config.toml` and
  `config.toml.example`.
- Each strategy implements `detect()` and `get_optimizable_params()`.
- `tests/test_strategies.py` parametrizes all exported strategies and checks the
  basic strategy contract.

| # | setup_id | Family / profile | Runtime status | Main implementation evidence |
|---|---|---|---|---|
| 1 | `structure_pullback` | continuation / trend_follow | Implemented, registered, config-backed | EMA/pullback/regime/volume checks; structural target helper |
| 2 | `structure_break_retest` | breakout / breakout_acceptance | Implemented, registered, config-backed | Breakout/retest detection, swing point checks |
| 3 | `wick_trap_reversal` | reversal / countertrend_exhaustion | Implemented, registered, config-backed | Wick trap and confirmation checks |
| 4 | `squeeze_setup` | breakout / breakout_acceptance | Implemented, registered, config-backed | BB/KC squeeze, volume, OI/funding crowding checks |
| 5 | `ema_bounce` | continuation / trend_follow | Implemented, registered, config-backed | EMA bounce, ADX, TP/risk checks |
| 6 | `fvg_setup` | continuation / trend_follow | Implemented, registered, config-backed | Uses `latest_fvg_zone()` |
| 7 | `order_block` | continuation / trend_follow | Implemented, registered, config-backed | Uses `latest_order_block()` |
| 8 | `liquidity_sweep` | reversal / countertrend_exhaustion | Implemented, registered, config-backed | Uses `latest_liquidity_sweep()` |
| 9 | `bos_choch` | breakout / breakout_acceptance | Implemented, registered, config-backed | Uses `latest_structure_break()` and external swing stop anchors |
| 10 | `hidden_divergence` | continuation / trend_follow | Implemented, registered, config-backed | Hidden divergence, context bias, delta confirmation |
| 11 | `funding_reversal` | reversal / countertrend_exhaustion | Implemented, registered, config-backed | Only strategy with `requires_funding=True` |
| 12 | `cvd_divergence` | reversal / countertrend_exhaustion | Implemented, registered, config-backed | CVD/delta divergence checks |
| 13 | `session_killzone` | breakout / breakout_acceptance | Implemented, registered, config-backed | Hardcoded killzone windows plus momentum/volume checks |
| 14 | `breaker_block` | breakout / breakout_acceptance | Implemented, registered, config-backed | Uses `latest_breaker_block()` |
| 15 | `turtle_soup` | reversal / countertrend_exhaustion | Implemented, registered, config-backed | False-breakout detection and 15m confirmation |
| 16 | `vwap_trend` | continuation / trend_follow | Implemented, registered, config-backed | VWAP reclaim continuation with 1h bias, ADX, volume, structural targets |
| 17 | `supertrend_follow` | continuation / trend_follow | Implemented, registered, config-backed | SuperTrend 15m+1h alignment plus EMA pullback and structural targets |
| 18 | `price_velocity` | breakout / breakout_acceptance | Implemented, registered, config-backed | ROC/body velocity breakout with volume and RSI guard |
| 19 | `volume_anomaly` | breakout / breakout_acceptance | Implemented, registered, config-backed | Volume spike impulse candle with close-position guard |
| 20 | `volume_climax_reversal` | reversal / countertrend_exhaustion | Implemented, registered, config-backed | Donchian sweep/reclaim, wick ATR, volume climax, RSI exhaustion |
| 21 | `keltner_breakout` | breakout / breakout_acceptance | Implemented, registered, config-backed | Keltner channel breakout with ADX, volume, 1h bias, structural targets |

Inference:

- "Real" is confirmed only in the software sense: the strategies are concrete,
  importable, registered, config-backed, and covered by contract tests.
- Trading validity, edge, and live robustness remain unverified without live
  replay/backtest evidence per strategy.

## SMC Implementation

Confirmed files:

- `bot/setups/smc.py` defines `SMCZone`, `SMCMode`, `ZoneState`, normalization
  helpers, and the main SMC functions.
- Implemented helpers include `fvg`, `swing_highs_lows`, `bos_choch`,
  `order_blocks`, `liquidity_pools`, `latest_fvg_zone`,
  `latest_order_block`, `latest_structure_break`, `latest_liquidity_sweep`,
  and `latest_breaker_block`.
- `swing_highs_lows(mode="live_safe")` uses the local `_swing_points` path and
  avoids the edge-adjustment behavior used for `offline_parity`.
- `bos_choch()` only emits resolved structures with `BrokenIndex`, then
  `latest_structure_break()` walks backward and returns the latest resolved BOS
  or CHOCH as an `SMCZone`.
- SMC tests in `tests/test_smc_helpers.py` check schema/length and one FVG
  invalidation case.

Assessment:

- Confirmed: the SMC layer is implemented and consumed by five strategies.
- Unverified: full parity against an external SMC reference is not proven by
  current tests. Existing tests are useful but narrow.
- Risk: BOS/CHoCH semantics depend heavily on swing marker generation and the
  requirement that structures are already broken/resolved.

## BOS/CHoCH 113:5 Imbalance

Confirmed source of the number:

- `logs/70_final_validation.md` records a 30-minute final run where
  `data.external_swing_stop_missing_short` occurred 113 times and
  `data.external_swing_stop_missing_long` occurred 5 times.

Confirmed telemetry parse:

- Parsed file:
  `data/bot/telemetry/runs/20260502_110946_22548/analysis/strategy_decisions.jsonl`.
- `bos_choch` total decisions: 502.
- Status counts: 277 rejects, 225 signals.
- Decision reasons:
  - `pattern.raw_hit`: 225
  - `data.external_swing_stop_missing_short`: 113
  - `pattern.no_choch_detected`: 85
  - `pattern.insufficient_swing_points`: 74
  - `data.external_swing_stop_missing_long`: 5
- Direction counts in decision telemetry: `short=169`, `long=56`, `n/a=277`.
- `external_swing_stop_missing_short` was concentrated in four symbols:
  `HYPEUSDT=35`, `AIOTUSDT=32`, `ORDIUSDT=30`, `SKYAIUSDT=16`.
- `external_swing_stop_missing_long` was only `AXLUSDT=5`.

Confirmed code path:

- In `bot/strategies/bos_choch.py`, short candidates require an external swing
  high above current price before the structure break index.
- Long candidates require an external swing low below current price.
- Missing anchors reject before a signal is built.

Inference:

- The 113:5 imbalance is consistent with the external stop-anchor rule, not a
  random telemetry artifact.
- The short-side imbalance is concentrated in a small set of symbols and likely
  appears when CHOCH candidates occur while no qualifying external swing high
  exists above current price.
- This may be a deliberate safety filter or an over-strict stop model. Current
  evidence is insufficient to label it a bug without replay/backtest comparison.

Recommended next verification:

- Add a focused replay test that captures the four high-count symbols around
  `20260502_110946_22548` and records price, structure break index, external
  swing markers, and selected/absent stop anchor.
- Compare current "reject when no external stop" behavior with a conservative
  fallback such as ATR-based invalidation, but do not change live logic without
  replay evidence.

## Magic Numbers

Confirmed hotspots from AST numeric-literal scan:

- `bot/scoring.py`: scoring and confluence thresholds.
- `bot/features.py`: indicator periods, session windows, volatility windows,
  cache sizes, and market-regime thresholds.
- `bot/market_data.py`: REST weights, request limits, cooldowns, and TTLs.
- `bot/universe.py`: shortlist/liquidity/ranking thresholds.
- `bot/application/symbol_analyzer.py`: history fetch sizes, crowding limits,
  alignment penalties, and filter thresholds.
- `bot/public_intelligence.py`: public-intelligence feature thresholds.
- `bot/ws_manager.py`: reconnect timings, stream/message limits, cache sizes,
  and latency thresholds.
- Strategy-level hotspots include `structure_pullback`, `session_killzone`,
  `squeeze_setup`, `fvg`, `order_block`, and `bos_choch`.

High-impact examples:

- `bot/features.py`: Ichimoku periods 9/26/52, broad session windows
  0-8/7-16/13-22 plus overlap 13-16, indicator windows
  14/20/21/22/96/210, cache size 500.
- `bot/ws_manager.py`: proactive reconnect at 23h50m, stream limit 300,
  incoming message safety limit 8/sec, subscribe message limit 4/sec.
- `bot/config.py`: many of these values are already configurable through
  Pydantic fields; others remain literal in implementation.
- `bot/strategies/session_killzone.py`: killzone hours are now config-driven
  numeric parameters with defaults London 7-10, NY 13-16, Asia 0-3, Overlap
  13-16; remaining literals include ADX threshold 18, last-3-bars momentum,
  and scan-20 range.
- `bot/strategies/bos_choch.py`: external swing lookback and stop-anchor
  fallback behavior materially affect rejection distribution.

Recommendation:

- Do not mechanically externalize every literal. Prioritize literals that change
  strategy behavior, live API safety, or operational limits.
- First remaining candidates: BOS/CHoCH external swing stop policy,
  symbol/history scan windows, session momentum/ADX thresholds, and public WS
  operational limits.

## Unused or Underused Features

Ichimoku:

- `bot/features.py` explicitly labels Ichimoku as unused by strategies and
  emits `ichi_tenkan`, `ichi_kijun`, `ichi_senkou_a`, `ichi_senkou_b`.
- `bot/features_structure.py` and `bot/features_advanced.py` also implement
  Ichimoku lines.
- `bot/public_intelligence.py` references Ichimoku columns as part of a feature
  surface check.
- No strategy directly references `ichi_*` columns in the current source scan.

Assessment:

- Confirmed unused by strategies.
- Not entirely dead code because tests and public-intelligence feature-surface
  code reference the columns.
- If removed, update feature contracts/tests first. If kept, decide whether any
  strategy should consume it; otherwise it is feature-surface overhead.

Session:

- `bot/features.py` emits broad session columns:
  Asia `0 <= hour < 8`, London `7 <= hour < 16`, NY `13 <= hour < 22`, and
  Overlap `13 <= hour < 16`.
- `bot/strategies/session_killzone.py` still evaluates bar time directly rather
  than consuming these feature columns, but its narrower killzones are now
  config-driven: London `(7,10)`, NY `(13,16)`, Asia `(0,3)`, Overlap `(13,16)`.
- The previous pre-guard indexed read of `prepared.work_15m.item(-1, "time")`
  was fixed; empty frames now reject with `insufficient_15m_bars`.

Assessment:

- Confirmed duplication remains: broad session feature columns and killzone
  strategy windows are separate implementations.
- Confirmed fixed: pre-guard `item(-1, "time")` no longer raises on empty 15m
  frames.

## Priority Assets and Sessions

Confirmed from user/project instruction:

- Priority assets: BTC, ETH, XAU, XAG.
- Trading sessions to model: Asia, London, NY, Overlap.

Current code state:

- `config.toml`, `config.toml.example`, and `UniverseConfig` now prioritize
  pinned symbols `BTCUSDT`, `ETHUSDT`, `XAUUSDT`, and `XAGUSDT`.
- Session features include Asia/London/NY/Overlap.
- `session_killzone` includes Asia/London/NY/Overlap killzones through
  numeric config parameters.

Inference:

- Overlap is now a first-class feature column and killzone label. The remaining
  inference is that no strategy consumes `session_overlap` directly yet.

## 2026-05-03 Full Phase Recheck

Reflection:

- Root cause of the incomplete previous pass: the first audit did not re-read
  the whole markdown corpus, did not follow the prompt phase checklist, and
  trusted stale audit sections without marking them as historical.
- Correction made in this pass: all markdown files were enumerated, current code
  was checked against stale docs, targeted regressions were run before and after
  code changes, and this map was revised after verification evidence.

Markdown corpus review:

- Markdown files scanned: 63.
- Groups: root/hidden 12, `bot/` 6, `docs/` 16, `logs/` 10, `data/` 15,
  `scripts/` 2, `tests/` 1, `reports/` 1.
- Key current documents read/checked: `AGENTS.md`, `.codex.md`,
  `codex_agent_prompt_v4.md`, `CODEX.md`, `Crypto-Analytic-Signal-Bot-AUDIT-v2.md`,
  `docs/REMEDIATION_PLAN.md`, `docs/v2_cycle_after_v1.md`,
  `docs/strategy_audit_2026-04-28.md`, `docs/shortlist_audit_2026-04-28.md`,
  `docs/FEATURES_IO_CONTRACT.md`, `docs/ws_runtime_audit.md`,
  `reports/binance_endpoint_registry.md`, and latest startup/public-intelligence
  reports under `data/bot/session/reports/`.
- Important stale-doc corrections:
  - `docs/strategy_audit_2026-04-28.md` still contained historical "critical"
    TP contradictions and `_build_signal` caveats; these were updated as
    historical/stale where current code disagrees.
  - `docs/FEATURES_IO_CONTRACT.md` did not list session overlap fields; it now
    does.
  - `docs/dependency_audit_2026-04-28.md` now states that runtime feature
    generation is pure Polars even with `polars_ta` installed.

Codebase scan:

- Python files scanned/enumerated: 210 after the phase-5.3 strategy expansion and
  current ignored paths.
- Groups: `bot/` 140, `tests/` 55, `scripts/` 15, root scripts 4.
- Routing files found: root plus 8 nested `AGENTS.md` files
  (`bot/`, `bot/application/`, `bot/setups/`, `bot/strategies/`, `bot/tasks/`,
  `bot/telegram/`, `scripts/`, `tests/`).
- Source/config/data inventory command covered 278 files:
  `rg --files bot scripts tests docs data config.toml config.toml.example requirements.txt CODEX.md PROJECT_MAP.md AGENTS.md README.md`.
- Active import target for `bot.setups` is confirmed as
  `bot/setups/__init__.py`; the sibling `bot/setups.py` remains a legacy/stale
  file in the tree and is not the import target.

Skill/phase matrix:

| Phase / skill | Status | Evidence |
|---|---|---|
| `code_audit` | executed | Python/Markdown corpus enumerated; runtime import path checked; strategy registry count 21. |
| `api_verify` | executed | Official Binance WS docs checked; live WS smoke connected to `/public/stream` and `/market/stream`. |
| `strategy_analyze` | executed | Original 15 strategies plus 6 phase-5.3 strategies imported/config-aligned; synthetic strategy tests pass. |
| `config_audit` | executed | `python scripts/validate_config.py` passed after config changes. |
| `telemetry_review` | executed | Runtime audit parsed run `20260502_110946_22548`; BOS/CHoCH 113:5 source confirmed. |
| `smc_verify` | executed | SMC helper tests passed; BOS/CHoCH stop selector now has focused tests. |
| `competitor_study` | executed | Freqtrade, Hummingbot, Jesse, and NautilusTrader official repos/docs were checked for architecture patterns. |

Code changes from the recheck:

- `bot/features.py`, `bot/features_core.py`: fixed pure-Polars ADX scale. The
  old path let the first `NaN` in `dx` propagate through `ewm_mean()`, then
  filled the whole ADX series with `0.0`. This made ADX-based strategies and
  diagnostics misread live trend strength.
- `bot/config.py`: `strategy_concurrency` and `strategy_timeout_seconds` are now
  declared in `RuntimeConfig`; previously `config.toml` contained these keys but
  Pydantic ignored them, so `SignalEngine` used fallback defaults.
- `bot/config.py`: phase-5.3 setup ids are now declared in `_ALL_SETUP_IDS` and
  `SetupConfig`; without this, new strategy files/config rows were present but
  disabled at runtime.
- `bot/strategies/vwap_trend.py`, `supertrend_follow.py`,
  `price_velocity.py`, `volume_anomaly.py`, `volume_climax_reversal.py`, and
  `keltner_breakout.py`: implemented six new signal-only setup detectors.
- `bot/universe.py`: shortlist `strategy_fits` now routes trend, breakout,
  top-liquidity, and reversal symbols into the new phase-5.3 strategies.
- `bot/market_data.py`: `/futures/data/*` request pacing is configurable via
  `runtime.futures_data_request_limit_per_5m` and defaults to 300/5m rather
  than the official 1000/5m ceiling.
- `bot/application/symbol_analyzer.py`: funnel telemetry now includes
  `rejects_by_stage`, `rejects_by_setup`, `reject_reasons_by_stage`, and
  `reject_reasons_by_setup`.
- `bot/application/oi_refresh_runner.py`, `bot/application/cycle_runner.py`:
  emergency fallback analysis now performs a bounded OI/L-S/funding context
  warmup when the public REST cache is cold/stale, reusing the existing
  rate-limited OI refresh runner.
- `scripts/live_check_pipeline.py`: added read-only live pipeline smoke for
  `prepare -> strategy -> confirmation -> filters` without Telegram delivery.
- `scripts/live_check_enrichments.py`: premium basis stats and depth/microprice
  checks are opt-in so the script does not fail by default on data streams the
  runtime intentionally keeps restricted.
- `bot/features.py`: restored `_HAS_TALIB=False`, disabled runtime
  `polars_ta` backend, restored `CORE_API`/`ADVANCED_API`/`OSCILLATORS_API`,
  and added `session_overlap` and `session_overlap_vol_20`.
- `bot/features_core.py`: aligned decomposed core feature column ordering with
  legacy `_prepare_frame`.
- `bot/ml/filter.py`: live-mode `SignalClassifier` fallback artifacts are
  blocked by guardrail instead of silently loading through the fallback path.
- `bot/scoring.py`: `_risk_reward_quality(...)` now tolerates narrow test
  settings objects without assuming a full `BotSettings.filters` tree.
- `bot/strategies/session_killzone.py`: guarded empty/missing-time frames,
  added config-driven Asia/London/NY/Overlap windows, and retained precise
  rejection reasons.
- `bot/strategies/bos_choch.py`: extracted external stop selector diagnostics
  without changing signal policy.
- `bot/strategies/wick_trap_reversal.py`: restored backward-compatible
  `wick_atr_threshold` alias propagation to `wick_through_atr_mult`.
- `bot/config.py`, `config.toml`, `config.toml.example`: pinned priority assets
  aligned to BTC/ETH/XAU/XAG and session killzone tunables added.
- `bot/application/shortlist_service.py`: fallback reason constants and
  structured shortlist fallback telemetry restored.
- Tests added/updated for feature parity, session overlap windows, empty
  session killzone frames, and BOS/CHoCH stop selector diagnostics.

Competitor study findings:

- Freqtrade emphasizes backtesting/dry-run, pairlists, Telegram/WebUI control,
  and explicit risk disclaimers. Applicable here: keep signal-only behavior and
  strengthen replay/backtest evidence before changing strategy policy.
- Hummingbot emphasizes standardized exchange connectors over strategy-specific
  API calls. Applicable here: preserve the Binance public USD-M boundary and
  keep REST/WS routing centralized.
- Jesse emphasizes research/backtest/live workflow separation for strategies.
  Applicable here: create replay fixtures before changing BOS/CHoCH stop policy.
- NautilusTrader emphasizes deterministic event-driven parity between research
  and live deployment. Applicable here: keep event-driven strategy contracts and
  telemetry stable across live/replay.

## Verification Log

Dependency checks:

- `python --version` -> Python 3.13.13.
- `python -m pip install -r requirements.txt --dry-run` -> passed.
- `python -m pip install --dry-run --only-binary=:all: -r requirements.txt` -> passed.
- `rg --files -g "requirements*.txt"` -> only `requirements.txt`.
- `rg -n "requirements-(dev-live|frozen|modern|optional)\.txt"` -> no refs.

Prompt file checks:

- `codex_agent_prompt_v4.md` exists.
- `CODEX.md` preserves the full original prompt text and appends a local
  verification addendum. It is intentionally no longer byte-identical to
  `codex_agent_prompt_v4.md`.

Project audit checks:

- `from bot.strategies import STRATEGY_CLASSES; len(STRATEGY_CLASSES)` -> 21.
- Strategy metadata print confirmed family/profile/context/min bars.
- TOML parse confirmed all 21 strategy ids exist in `[bot.setups]` and
  `[bot.filters.setups]` for both `config.toml` and `config.toml.example`.
- `rg` scan confirmed no direct strategy use of `ichi_*`.
- `rg` scan confirmed session feature and session-killzone duplication; code
  now includes first-class overlap fields and config-driven killzone windows.
- Telemetry parse confirmed BOS/CHoCH 113:5 stop-anchor imbalance in the final
  run referenced by logs.
- `python scripts\validate_config.py` -> passed after config changes.
- `python -m ruff check` on touched Python files -> passed.
- `python -m pytest -q tests/test_features_decomposition_parity.py tests/test_features.py tests/test_strategies.py tests/test_smc_helpers.py`
  -> 30 passed after feature/session/BOS diagnostic changes.
- `python -m pytest -q` -> 225 passed.
- `python scripts\live_check_strategies.py --symbols BTCUSDT ETHUSDT XAUUSDT XAGUSDT --limit 4 --concurrency 2`
  -> detector_runs=60, prepared_ok=4, strategy_errors=[].
- `python scripts\live_check_strategies.py --symbols-from-run 20260502_110946_22548 --limit 45 --concurrency 3`
  -> detector_runs=675, prepared_ok=45, strategy_errors=[], strategy hits
  across BOS/CHoCH, structure_pullback, FVG, breaker_block, CVD, EMA bounce,
  wick trap, and turtle soup.
- `python -m pytest tests\test_features.py::test_prepare_frame_keeps_rsi_adx_on_indicator_scale tests\test_features_group_contracts.py::test_group_contract_outputs tests\test_config_runtime.py -q`
  -> 3 passed after the ADX/config fixes.
- `python -m pytest tests\test_config_runtime.py tests\test_market_data_limits.py tests\test_symbol_analyzer_telemetry.py tests\test_strategies.py tests\test_sanity.py::test_strategy_registry_contains_extended_setups tests\test_backtest_engine.py::test_backtester_supports_lifecycle_metrics_for_all_live_setups -q`
  -> 39 passed after the phase-5.3 strategy, REST, WS, and telemetry changes.
- `rg --files bot scripts tests docs data config.toml config.toml.example requirements.txt CODEX.md PROJECT_MAP.md AGENTS.md README.md`
  -> 278 files in the audited surface.
- `python scripts\live_check_pipeline.py --symbols BTCUSDT ETHUSDT XAUUSDT XAGUSDT --limit 4 --concurrency 2`
  exposed two issues before completing: the script used a missing runtime
  attribute and the eager `/futures/data/basis` warmup triggered Binance HTTP
  418. The script was changed to safe lite-context by default. No further live
  Binance REST verification was run in this pass because of the ban window.

## 2026-05-03 Current Session Addendum

Git/GitHub:

- Confirmed branch: `main`.
- Confirmed remote: `https://github.com/TrololoBird/Crypto-Analytic-Signal-Bot.git`.
- `git push origin main` returned `Everything up-to-date`; there were no
  pre-existing local changes to push before this session's audit work.

Fresh project scan:

- Required local `AGENTS.md` files read for root, `bot/`, `bot/strategies/`,
  `bot/setups/`, `scripts/`, and `tests/`.
- `rg --files bot scripts tests docs data logs config.toml config.toml.example PROJECT_MAP.md AGENTS.md pyproject.toml requirements.txt`
  returned 287 tracked/audited paths in the requested surface.
- Confirmed mismatch with the prompt: there is no `bot/filters/` directory in
  the current repository; filter logic is in `bot/filters.py`.
- Runtime import check still reports 21 strategy classes, not the 15 listed in
  the prompt.

Fresh Binance verification:

- Official docs checked on 2026-05-03:
  - `https://developers.binance.com/docs/derivatives/usds-margined-futures/websocket-market-streams`
  - `https://developers.binance.com/docs/derivatives/usds-margined-futures/websocket-market-streams/Live-Subscribing-Unsubscribing-to-streams`
  - `https://developers.binance.com/docs/derivatives/usds-margined-futures/market-data/rest-api/Order-Book`
  - `https://developers.binance.com/docs/derivatives/usds-margined-futures/market-data/rest-api/Compressed-Aggregate-Trades-List`
- Live REST smoke:
  - `/fapi/v1/time` -> HTTP 200, keys `serverTime`.
  - `/fapi/v1/depth?symbol=BTCUSDT&limit=5` -> HTTP 200, keys
    `lastUpdateId`, `E`, `T`, `bids`, `asks`.
  - `/fapi/v1/aggTrades?symbol=BTCUSDT&limit=1` -> HTTP 200, list payload.
  - `/fapi/v1/premiumIndex?symbol=BTCUSDT` -> HTTP 200, mark/funding keys.
- Live WS smoke:
  - `wss://fstream.binance.com/public/stream?streams=btcusdt@depth5@100ms`
    emitted combined payload keys `stream` and `data`.
  - `wss://fstream.binance.com/market/stream?streams=btcusdt@markPrice`
    emitted combined payload keys `stream` and `data`.
- Live `exchangeInfo` confirmed priority symbols:
  `BTCUSDT`, `ETHUSDT`, `XAUUSDT`, `XAGUSDT`; XAU/XAG are
  `TRADIFI_PERPETUAL` contracts.

Fresh runtime/live checks:

- `python scripts\validate_config.py` -> passed.
- `python -m pytest -q tests\test_strategies.py::test_bos_choch_external_stop_selector_reports_side_filtering tests\test_strategies.py::test_bos_choch_external_stop_selector_diagnoses_missing_anchor tests\test_smc_helpers.py`
  -> 9 passed.
- `python scripts\live_check_strategies.py --symbols BTCUSDT ETHUSDT XAUUSDT XAGUSDT --limit 4 --concurrency 2`
  -> prepared_ok=4, detector_runs=84, strategy_errors=[].
- `python scripts\live_check_binance_api.py --symbols BTCUSDT ETHUSDT XAUUSDT XAGUSDT --warmup-seconds 6 --reconnect-wait-seconds 6`
  -> REST checks passed, fresh WS ticker/mark/book/kline caches populated, and
  forced market reconnect recovered.
- `python scripts\live_check_pipeline.py --symbols BTCUSDT ETHUSDT XAUUSDT XAGUSDT --limit 4 --concurrency 2 --no-warm-context`
  -> prepared_ok=4, detector_runs=84, raw_hits=12, candidates=0,
  dry_selected=0. This is a calibration fact, not a runtime failure.
- `python scripts\live_check_enrichments.py --symbols BTCUSDT ETHUSDT --warmup 8`
  -> 12/12 critical fields populated for BTC/ETH. Premium slope/zscore were
  not required in this run and remained `None`.

Fresh bug fix:

- Confirmed `_FrameCache` bug with a red test: two frames with the same
  `(symbol, interval, close_time)` but different historical windows returned
  the first cached prepared frame.
- Fixed `bot/features.py` cache keys to include symbol, interval, frame row
  count, first close time, and last close time.
- Added regression test
  `tests/test_features.py::test_cached_prepare_frame_distinguishes_same_close_time_with_different_history`.
- Verification:
  - Red before fix: the new test failed with both prepared heights equal.
  - Green after fix: the new test passed.
  - `python -m ruff check bot\features.py tests\test_features.py` -> passed.

BOS/CHoCH recheck:

- Parsed run `20260502_110946_22548`; counts remain:
  `data.external_swing_stop_missing_short=113`,
  `data.external_swing_stop_missing_long=5`,
  `pattern.no_choch_detected=85`,
  `pattern.insufficient_swing_points=74`,
  `pattern.raw_hit=225`.
- The old telemetry run lacks the new selector diagnostic fields except
  `external_swing_lookback` and a market snapshot, so it cannot prove whether
  side-filtering or absent external markers dominated the historical 113 rejects.
- Current code path still supports the previous inference: short candidates
  require a valid external swing high above current price before the structure
  break index. I did not relax this live policy without replay evidence because
  that would change risk semantics.

Unused-feature recheck:

- `ichi_*` columns are still not referenced directly by strategy files.
- They are referenced by public-intelligence/feature-surface code and tests, so
  deleting them in this pass would be a contract change, not a safe
  performance-only cleanup.
- Session features are still duplicated with `session_killzone` direct time
  checks, but `session_overlap` exists and overlap killzone tests pass.

Competitor study:

- Added `docs/competitor_study_2026-05-03.md`.
- GitHub metadata was collected for the 10 prompt-named projects plus 10
  additional current/topical projects. Full README pulls were blocked by
  unauthenticated GitHub API rate limiting for most repositories, so the study
  is explicitly marked as metadata/README-summary level rather than deep code
  audit.

External references checked:

- Binance USD-M Futures Websocket Market Streams official docs:
  https://developers.binance.com/docs/derivatives/usds-margined-futures/websocket-market-streams
- Binance USD-M Futures REST market data docs:
  https://developers.binance.com/docs/derivatives/usds-margined-futures/market-data/rest-api
- Binance USD-M Futures Open Interest Statistics docs:
  https://developers.binance.com/docs/derivatives/usds-margined-futures/market-data/rest-api/Open-Interest-Statistics
- Binance USD-M Futures L/S Ratio docs:
  https://developers.binance.com/docs/derivatives/usds-margined-futures/market-data/rest-api/Long-Short-Ratio
- Binance USD-M Futures Basis docs:
  https://developers.binance.com/docs/derivatives/usds-margined-futures/market-data/rest-api/Basis
- Binance live subscribe/unsubscribe docs:
  https://developers.binance.com/docs/derivatives/usds-margined-futures/websocket-market-streams/Live-Subscribing-Unsubscribing-to-streams
- `websockets` 16.0 client docs:
  https://websockets.readthedocs.io/en/stable/reference/asyncio/client.html
- `websockets` PyPI metadata:
  https://pypi.org/project/websockets/
- `polars_ta` PyPI metadata:
  https://pypi.org/project/polars-ta/

## Residual Risk

- This audit did not run a full live bot session.
- Fresh post-filter candidate proof is still incomplete because live Binance
  REST checks hit HTTP 418 after an overly aggressive diagnostic basis warmup.
- This audit did not prove trading edge, expectancy, or low false-positive rate.
- SMC parity against an external reference remains partially tested, not proven.
- Dependency resolution was checked on the current Python 3.13 Windows
  environment; future package releases may change wheel availability.
