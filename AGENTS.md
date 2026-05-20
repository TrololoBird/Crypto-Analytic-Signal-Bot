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
