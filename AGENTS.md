# Crypto Signal Bot - Agent Routing

## Goal

Keep context small, but do not relearn the project from scratch. Start with this
file and `docs/AGENT_PLAYBOOK.md`, then verify the exact module/contract you are
touching against current code, configs, telemetry, live scripts, and official or
well-maintained external sources.

This repository has accumulated AI-generated docs, audits, scripts, and tests.
Those artifacts can drift. Treat them as clues, not proof.

## Repo Facts To Re-Verify Before Use

- Runtime entry: `main.py` -> `bot.cli.run()` -> `bot.application.bot.SignalBot`
- Main package: `bot/`
- Orchestration lives in `bot/application/bot.py`
- Core infra lives in `bot/core/`
- Strategies live in `bot/strategies/` and are exported via `bot/strategies/__init__.py`
- Shared setup helpers live in `bot/setups/`; `bot/setup_base.py` adapts them
  to the strategy engine.
- Config lives in `config.toml` and `config.toml.example`, parsed by `bot/domain/config.py`
- Persistence lives in `bot/core/memory/repository.py`
- Manual/live checks live in `scripts/live_*.py`
- Generated tests were removed from this workspace. They were not evidence that
  a strategy works in live market conditions.
- New long-running Codex sessions can start from
  `docs/CODEX_UNIVERSAL_PROMPT.md` after this file and
  `docs/AGENT_PLAYBOOK.md`.

## Global Rules

- Prefer `rg` and targeted reads. Do not read large files end-to-end unless you are editing them.
- Start from the module named in the task, then expand only through imports, callers, and direct contracts.
- If code, docs, `AGENTS.md`, and audit reports disagree, the precedence is:
  current runtime code and config first, then runtime logs/telemetry/live scripts,
  then official docs or high-quality GitHub sources for external/time-sensitive
  behavior, then local docs/instructions.
- Mark audit items as `confirmed`, `already-fixed/false`, `ambiguous`, or
  `deferred with reason`; do not apply stale audit recommendations blindly.
- When a task reveals doc or instruction drift, update the relevant docs in the
  same change or explicitly document why it was not safe to do so.
- Use Polars for dataframe work and prefer the installed Polars/polars_ta
  feature stack from `requirements.txt`. Do not introduce pandas-centric flows
  or duplicate indicator math inside strategies when the prepared Polars feature
  layer already provides the column.
- Keep I/O async. Avoid blocking calls inside async code.
- Preserve the logging style already used in the touched module. Do not mix in a new logging stack unless the task is a deliberate refactor.
- Strategy parameters belong in `config.toml` under `[bot.filters.setups]`; enable flags belong under `[bot.setups]`.
- Keep `config.toml.example` aligned when config surface changes.
- Persistence changes should go through `MemoryRepository`, not ad hoc files or duplicate stores.
- Never hardcode secrets or paste real API keys/tokens.
- Binance integration boundary is public USDⓈ-M market data only. Do not add signed REST endpoints, account/trade endpoints, `listenKey`, or user-data streams.
- Shortlist changes must preserve the `rest_full` vs `ws_light` split and keep fallback behavior explicit in telemetry.
- Runtime logging must classify problems instead of flooding the console with
  generic warnings: expected pacing/fallback belongs at info/debug; actionable
  failures belong at error/exception/critical with useful context.
- Do not disable a strategy because its metadata status says `experimental`,
  `beta`, or similar. Those labels are descriptive only. If a strategy does not
  signal, diagnose its data contract, gates, filters, config thresholds, and
  market-context dependencies.
- Do not delete code, docs, tests, or scripts by guess. Delete only after `rg`,
  import/call-path review, and runtime/config/doc reference checks show that the
  target is unused or actively misleading.

## Strategy Reliability Rules

- All setup ids exported by `bot/strategies/__init__.py::STRATEGY_CLASSES` are
  expected to be registered, enabled by config, and capable of producing either
  a signal or a precise `StrategyDecision` reason.
- A no-signal finding must be classified as one of:
  `missing_source_data`, `stale_enrichment`, `insufficient_history`,
  `required_feature_missing`, `filter_gate`, `threshold_too_strict`,
  `context_conflict`, `implementation_bug`, or `market_condition`.
- Fix upstream data collection when the detector needs public Binance data that
  the runtime does not populate yet. Do not fake unavailable data under a
  misleading strategy name; any diagnostic proxy must be explicitly labeled in
  signal reasons and scored lower than the real data source.
- Use official Binance docs for endpoint/stream behavior and inspect real
  GitHub implementations for strategy patterns before rewriting strategy logic.
- Keep signal-only scope: produce entry/SL/TP/context, never account access or
  order execution.
- Global trend conflict handling is family-aware: continuation/breakout/trend
  following can be hard-rejected when they fight confirmed 1h context, but
  countertrend exhaustion families should be score-penalized and still evaluated
  by confluence.

## Routing

- `bot/*.py`: shared package-level modules such as features, market data, tracking, delivery, telemetry, confluence, dashboard, metrics, market regime, and compatibility shims
- `bot/application/`: runtime orchestration and event wiring, including `ShortlistService`, `CycleRunner`, `HealthManager`, `TelemetryManager`, `DeliveryOrchestrator`, `FallbackRunner`, `KlineHandler`, and `OIRefreshRunner`
- `bot/core/`: event bus, engine, memory, diagnostics, analyzer, self-learner
- `bot/domain/`: Pydantic config, event, strategy, schema, and public feature contracts
- `bot/ml/`: canonical ML filter, guardrails, classifier, and training pipeline
- `bot/learning/`: walk-forward optimizer, regime-aware params, and outcome store helpers
- `bot/regime/`: HMM/GMM/composite regime detectors
- `bot/strategies/`: individual setup detectors
- `bot/setups/`: shared strategy helpers; do not recreate a sibling
  `bot/setups.py` module.
- `bot/telegram/`: Telegram queue/sender infrastructure
- There is intentionally no `tests/` contract in this workspace. Verification
  should use compile/import checks, config validation, telemetry diagnostics,
  and read-only live scripts.
- `scripts/`: manual/live validation scripts

## Required Work Pattern

1. Read the nearest local `AGENTS.md` as context, not authority.
2. Identify the exact entry point, contract, and affected callers.
3. Verify any audit/doc claim against the current code or official external
   docs before changing behavior.
4. Edit the smallest coherent set of files.
5. Verify with the narrowest relevant check first: grep, import path review,
   compile/import check, config validation, telemetry replay, diagnostic script,
   or live script when the task requires external validation.
6. In summaries, separate confirmed facts from assumptions, inferences,
   ambiguous claims, and unverified follow-up risks.

## Changelog (agent-readable)

### 2026-05-21 - Strategy spec diagnostics and docs alignment

Reason for session: run a read-only diagnostic pass over the 38-strategy
runtime surface, classify strategy/data-contract risks, and align drifted docs
with the current code contract.

Changes:
- `docs/FEATURES_IO_CONTRACT.md`: clarified that current `prepare_symbol()`
  requires warm `5m`, `15m`, `1h`, and `4h` frames before returning
  `PreparedSymbol`.
- `docs/OPERATIONS.md`: aligned historical-kline operating notes with the
  same four-timeframe warmup contract.
- `bot/public_intelligence.py` and `bot/domain/config.py`: removed the dead
  Binance options eAPI endpoint literals/config toggle so the runtime codebase
  contains no `eapi.binance.com` endpoint string.
- `bot/market_data.py`: normalized private/auth REST parameter deny-list keys
  to lowercase so `listenkey`/`apikey` checks match the lowercased request
  parameters.
- Existing working tree changes before this session already included the
  shared `bot/strategies/spec_patterns.py` strategy-spec layer and public-data
  boundary updates; this session verified them rather than rewriting them.

Verified:
- `python -m compileall bot` -> exit 0.
- `python -m scripts.validate_config` -> `[OK] All checks passed`.
- `python -m scripts.live_check_strategies --limit 35 --concurrency 4` ->
  34 prepared symbols, 1292 detector runs, `strategy_errors=[]`, 28 strategies
  with detector hits.
- `BOT_NOTIFIER_PROVIDER=none python -m scripts.live_check_strategies
  --limit 15 --concurrency 3` -> 15 prepared symbols, 570 detector runs,
  `strategy_errors=[]`, 26 strategies with detector hits.
- `python -m scripts.live_check_enrichments --symbols BTCUSDT ETHUSDT
  --warmup 8 --require-depth` -> 22/22 critical enrichment fields populated;
  `depth_imbalance_source=l2_depth`, `orderflow_source=agg_trade`.

Known remaining limitations:
- `scripts/live_check_strategies.py` is a REST-oriented detector-surface check
  and does not warm WebSocket L2 depth or force-order streams. Orderbook and
  liquidation strategy misses in that script must be compared with
  `scripts.live_check_enrichments` or a running pipeline before being classified
  as strategy bugs.
- Zero-hit sets varied between the 35-symbol and later 15-symbol REST-surface
  runs as market state changed. Treat repeated single-code rejects as triage
  candidates, not as confirmed broken strategy logic without a WS/pipeline
  comparison and per-gate evidence.

### 2026-05-21 - Zero-hit detector remediation

Reason for session: the previous session reported 26/38 strategies with
`detector_hits > 0`; the fresh baseline for this session found 15 zero-hit
strategies on a 20-symbol sample. This session targeted only zero-hit detector
participation and avoided infrastructure refactors.

Changes:
- `bot/strategies/absorption.py`, `aggression_shift.py`, `atr_expansion.py`,
  `bb_squeeze.py`, `fvg.py`, `hidden_divergence.py`,
  `rsi_divergence_bottom.py`, `squeeze_setup.py`,
  `stop_hunt_detection.py`, `structure_pullback.py`,
  `volume_anomaly.py`, `volume_climax_reversal.py`,
  `wick_trap_reversal.py`, and `wyckoff_spring.py`: strict
  `spec_patterns` misses now fall through to the already existing
  config-driven fallback detector instead of returning an early reject.
- `bot/strategies/depth_imbalance.py`: explicit public `l1_book` and
  `rest_book_l1` sources are accepted as lower-scored orderbook proxies rather
  than being hard-rejected as missing L2 depth.
- `bot/strategies/whale_walls.py`: when persistent L2 wall pressure is absent,
  explicit public depth imbalance can produce a lower-scored, labeled
  `*_depth_proxy` signal.
- `bot/strategies/liquidation_heatmap.py`: added a labeled
  `source=volume_wick_proxy` fallback using existing public OHLCV proxy
  parameters; `force_order` remains reserved for real force-order stream data.
- `docs/STRATEGIES.md`: added the zero-hit remediation log and documented the
  remaining market-condition zeroes.

Verified:
- `python -m compileall bot/strategies` -> exit 0.
- `python -m scripts.validate_config` -> `[OK] All checks passed`.
- `python -m scripts.live_check_strategies --limit 20 --concurrency 3` ->
  20 prepared symbols, 760 detector runs, `strategy_errors=[]`, 35/38
  strategies with `detector_hits > 0`.

Known remaining limitations:
- `funding_reversal`: `market_condition`; sampled funding rates were below the
  configured `funding_threshold=0.001`, so `indicator.funding_not_extreme` is
  expected until crowding appears.
- `ls_ratio_extreme`: `market_condition`; current symbols were either not
  extreme enough or lacked the required price-position confirmation for a
  contrarian signal.
- `supertrend_follow`: `market_condition`; current symbols lacked a valid
  SuperTrend pullback, or failed volume/ADX gates, so no detector bug was
  confirmed.

## Strategy Status - 2026-05-21

| Strategy | Family | TF | Status | Zero reason if applicable | Fix |
|---|---|---|---|---|---|
| absorption | orderflow | 15m | LIVE | implementation_bug: spec miss hid fallback | fallthrough fixed |
| aggression_shift | orderflow | 15m | LIVE | implementation_bug: spec miss hid fallback | fallthrough fixed |
| altcoin_season_index | multi_asset | 15m/market | LIVE | - | - |
| atr_expansion | volatility | 15m | LIVE | implementation_bug: spec threshold hid fallback | fallthrough fixed |
| bb_squeeze | volatility | 15m | LIVE | implementation_bug: last-bar spec hid memory window | fallthrough fixed |
| bos_choch | breakout | 15m | LIVE | - | - |
| breaker_block | liquidity | 15m | LIVE | - | - |
| btc_correlation | multi_asset | 15m/market | LIVE | - | - |
| cvd_divergence | orderflow | 15m | LIVE | - | - |
| depth_imbalance | orderbook | 15m | LIVE | source_gate: REST/L1 public source hard-rejected | labeled proxy |
| ema_bounce | continuation | 15m | LIVE | - | - |
| funding_reversal | reversal | 15m+funding | MARKET_CONDITION | funding not extreme | documented |
| fvg_setup | continuation | 15m | LIVE | implementation_bug: spec retest hid SMC zone fallback | fallthrough fixed |
| hidden_divergence | continuation | 15m+1h | LIVE | implementation_bug: spec miss hid swing scan | fallthrough fixed |
| indicator_divergence | reversal | 15m | LIVE | - | - |
| keltner_breakout | volatility | 15m | LIVE | - | - |
| liquidity_sweep | liquidity | 15m | LIVE | - | - |
| liquidation_heatmap | liquidity | 15m | LIVE | implementation_bug: documented proxy params unused | volume_wick_proxy |
| ls_ratio_extreme | sentiment | 15m+futures data | MARKET_CONDITION | no extreme/price confirmation | documented |
| multi_tf_trend | continuation | 15m+1h+4h | LIVE | market state mixed in baseline | no code change |
| oi_divergence | sentiment | 15m+OI | LIVE | - | - |
| order_block | continuation | 15m | LIVE | - | - |
| price_velocity | momentum | 15m | LIVE | - | - |
| rsi_divergence_bottom | reversal | 15m | LIVE | implementation_bug: spec miss hid window detector | fallthrough fixed |
| session_killzone | session | 15m | LIVE | - | - |
| spread_strategy | orderbook | 15m | LIVE | - | - |
| squeeze_setup | breakout | 15m | LIVE | implementation_bug: spec miss hid squeeze fallback | fallthrough fixed |
| stop_hunt_detection | liquidity | 15m | LIVE | implementation_bug: spec miss hid sweep fallback | fallthrough fixed |
| structure_break_retest | breakout | 15m | LIVE | - | - |
| structure_pullback | continuation | 15m+1h | LIVE | implementation_bug: spec fib window hid fallback | fallthrough fixed |
| supertrend_follow | continuation | 15m+1h | MARKET_CONDITION | no current SuperTrend pullback/volume/ADX gate | documented |
| turtle_soup | liquidity | 15m | LIVE | - | - |
| volume_anomaly | momentum | 15m | LIVE | implementation_bug: spec miss hid recent-bar fallback | fallthrough fixed |
| volume_climax_reversal | reversal | 15m | LIVE | implementation_bug: spec miss hid reclaim fallback | fallthrough fixed |
| vwap_trend | continuation | 15m | LIVE | - | - |
| whale_walls | orderbook | 15m | LIVE | source_gate: missing persistent L2 wall pressure | labeled depth proxy |
| wick_trap_reversal | reversal | 15m+1h | LIVE | implementation_bug: spec miss hid 1h swing trap fallback | fallthrough fixed |
| wyckoff_spring | reversal | 15m | LIVE | implementation_bug: spec miss hid range sweep fallback | fallthrough fixed |
