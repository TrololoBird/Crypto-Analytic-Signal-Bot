# Crypto Signal Bot - Agent Routing

## Goal

Keep context small, but treat this file as routing context rather than source
of truth. The project is in active AI-generated development; instructions,
docs, audit reports, and code can drift. Verify claims against the current
code, tests, configs, logs, and official external docs before relying on them.

## Repo Facts To Re-Verify Before Use

- Runtime entry: `main.py` -> `bot.cli.run()` -> `bot.application.bot.SignalBot`
- Main package: `bot/`
- Orchestration lives in `bot/application/bot.py`
- Core infra lives in `bot/core/`
- Strategies live in `bot/strategies/` and are exported via `bot/strategies/__init__.py`
- Shared setup helpers live in `bot/setups.py` and `bot/setups/`
- Config lives in `config.toml` and `config.toml.example`, parsed by `bot/domain/config.py`
- Persistence lives in `bot/core/memory/repository.py`
- Regression tests are spread across `tests/test_*.py`; use targeted `rg` or
  `pytest --collect-only` instead of assuming one canonical regression file.
- Manual/live checks live in `scripts/live_*.py`

## Global Rules

- Prefer `rg` and targeted reads. Do not read large files end-to-end unless you are editing them.
- Start from the module named in the task, then expand only through imports, callers, and direct contracts.
- If code, docs, `AGENTS.md`, and audit reports disagree, the precedence is:
  current code and tests first, then runtime logs/configs, then official docs
  for external/time-sensitive behavior, then local docs/instructions.
- Mark audit items as `confirmed`, `already-fixed/false`, `ambiguous`, or
  `deferred with reason`; do not apply stale audit recommendations blindly.
- When a task reveals doc or instruction drift, update the relevant docs in the
  same change or explicitly document why it was not safe to do so.
- Use Polars for new dataframe work. Do not introduce pandas-centric flows.
- Keep I/O async. Avoid blocking calls inside async code.
- Preserve the logging style already used in the touched module. Do not mix in a new logging stack unless the task is a deliberate refactor.
- Strategy parameters belong in `config.toml` under `[bot.filters.setups]`; enable flags belong under `[bot.setups]`.
- Keep `config.toml.example` aligned when config surface changes.
- Persistence changes should go through `MemoryRepository`, not ad hoc files or duplicate stores.
- Never hardcode secrets or paste real API keys/tokens.
- Binance integration boundary is public USDⓈ-M market data only. Do not add signed REST endpoints, account/trade endpoints, `listenKey`, or user-data streams.
- Shortlist changes must preserve the `rest_full` vs `ws_light` split and keep fallback behavior explicit in telemetry.

## Routing

- `bot/*.py`: shared package-level modules such as features, market data, tracking, delivery, telemetry, confluence, dashboard, metrics, market regime, and compatibility shims
- `bot/application/`: runtime orchestration and event wiring, including `ShortlistService`, `CycleRunner`, `HealthManager`, `TelemetryManager`, `DeliveryOrchestrator`, `FallbackRunner`, `KlineHandler`, and `OIRefreshRunner`
- `bot/core/`: event bus, engine, memory, diagnostics, analyzer, self-learner
- `bot/domain/`: Pydantic config, event, strategy, schema, and public feature contracts
- `bot/ml/`: canonical ML filter, guardrails, classifier, and training pipeline
- `bot/learning/`: walk-forward optimizer, regime-aware params, and outcome store helpers
- `bot/regime/`: HMM/GMM/composite regime detectors
- `bot/strategies/`: individual setup detectors
- `bot/setups/`: shared strategy helpers
- `bot/tasks/`: background jobs and schedulers
- `bot/telegram/`: Telegram queue/sender infrastructure
- `tests/`: regression coverage
- `scripts/`: manual/live validation scripts

## Required Work Pattern

1. Read the nearest local `AGENTS.md` as context, not authority.
2. Identify the exact entry point, contract, and affected callers.
3. Verify any audit/doc claim against the current code or official external
   docs before changing behavior.
4. Edit the smallest coherent set of files.
5. Verify with the narrowest relevant check first: grep, import path review,
   targeted `pytest`, or a live script when the task actually requires external
   validation. Then broaden verification before release/push.
6. In summaries, separate confirmed facts from assumptions, inferences,
   ambiguous claims, and unverified follow-up risks.
