# CLAUDE.md — Project context for Claude Code agent

## What this project is

Crypto futures **signal-analytics** bot (no auto-trading, no authenticated Binance endpoints).
Signals only. Telegram delivery. Public Binance USDⓈ-M endpoints only.

## Active development — agents may change everything

**Not production-frozen.** Codebase is largely AI-generated; architecture, strategies, indicators, and config defaults are provisional until validated by live outcomes and external research.

Agents **may refactor the full stack** (packages, delivery path internals, monolith splits, indicator math, schema migrations) when needed for correctness. Do not preserve broken structure out of caution.

**Hard limits unchanged:** no auto-trading, no authenticated Binance APIs, delivery order `validate_signal_contract` → `hard_confluence_gate` → `deliver`.

## Runtime topology (read before editing anything)

**Entry:** `main.py` → `bot/cli.py` → `asyncio.run(_main())` → `SignalBot.start()` + `await bot.run_forever()`

**Hot path (latency-critical — do not add blocking sync I/O):**

```text
WS kline close → EventBus → SymbolAnalyzer → SignalEngine → DeliveryOrchestrator
  → validate_signal_contract() → hard confluence gate → deliver()
```

**Background tasks** (`SignalBot.run_forever()` in `bot/runtime/bot.py`):

- `event_bus`
- `shortlist_refresh`
- `heartbeat`
- `health_telemetry`
- `health_monitor`
- `emergency_fallback`
- `oi_refresh`
- `spot_companion`
- `tracking_review`
- `market_regime`
- `public_intelligence` (if intelligence enabled)
- `telegram_operator` (if operator console enabled)

**Also at startup** (`bot/cli.py` / `SignalBot.start()`): `daily_summary`, `preload_frames` (REST warmup), optional FastAPI dashboard.

## Schema discipline

- DB schema changes — new steps in `bot/migrations.py` only; never raw `ALTER` outside migrations
- Strategy IDs: removing from `STRATEGY_CLASSES` requires syncing `CATALOG_ENTRIES` + config

## Module ownership map

| Concern | Primary module | Notes |
|---------|----------------|-------|
| WS market data | `engine/market/ws.py` + `_ws_connection.py` + `_ws_parsers.py` | Shared kernel; public interface unchanged on `ws.py` |
| REST market data | `engine/market/rest_impl.py` + `_rest_circuit.py` + `_rest_frames.py` | Shared kernel; `RestCircuitMixin` on `RestHttpMixin` |
| Feature pipeline | `engine/features/prepare.py` → `prepare_frame.py` | Shared kernel; Polars hot path |
| Strategy execution | `bot/engine/engine.py` + `registry.py` | 42 strategies |
| Signal delivery | `bot/delivery/` (contract → filters → confluence → deliver) | Invariant order enforced |
| Persistence CRUD | `bot/persistence/repository/memory.py` | SQLite + parquet; inherits `AnalyticsMixin` |
| Persistence DDL (schema) | `bot/persistence/repository/_schema.py` | DDL strings only — imported by `memory.py` |
| Persistence analytics CRUD | `bot/persistence/repository/_analytics.py` | `AnalyticsMixin` inherited by `MemoryRepository` |
| Signal lifecycle | `bot/persistence/tracking.py` | `active_signals` / `signal_outcomes`; core state machine (~1k LOC post-G) |
| TP/SL review (tracking) | `bot/persistence/_tracking_review.py` | `TPSLReviewMixin` on `SignalTracker` |
| Telegram tracking IDs | `bot/persistence/_tracking_telegram.py` | `TelegramTrackingMixin` on `SignalTracker` |
| Analyzer gate functions | `bot/runtime/_analyzer_gates.py` | Family/context gate mixins for `SymbolAnalyzer` |
| Delivery ranking | `bot/runtime/_delivery_ranking.py` | `DeliveryRankingMixin` |
| Delivery watch recording | `bot/runtime/_delivery_watch.py` | `DeliveryWatchMixin` |
| DB schema migrations | `bot/migrations.py` **only** — sole writer of `schema_version` | |
| Config | `engine/domain/config.py` + `config.toml` | Shared kernel; `BotSettings` |
| Secrets | `engine/secrets.py` | Canonical: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` |
| Bot-side domain policies | `bot/policy/` (`mtf`, `catalog_guards`, `labels`, `delivery_policy`) | Import persistence/regime — NOT kernel material |
| Hunter project | `hunt/` (package `hunt_core`) | Standalone; **100% CCXT** market plane — see `hunt/docs/CCXT.md`; never imports `bot.*` or `engine.*`. **Two modules:** Deep (pinned majors + `/signal SYM`) + Scanner (universe). Shared spine: `hunt_core/signals/` (`Signal`, `setup_id` dedup, lifecycle states forming→signal→activated→tracking→closed). Both modules emit via `SignalEmitter`; no `deep_change_fingerprint`, no emission quota (`target_signal_rate` deleted). Deep plan authority: `hunt_core/prizrak/` (`verdict_v2` deleted in the fusion-engine migration — do not reintroduce or reference it). Scanner prep: `scanner/gate/_mission.assess_preparation_readiness`. Docs: `hunt/docs/SPEC_v5.1.md` (legacy 5-module pipeline spec — superseded by Prizrak; verify before trusting). |
| CLI | `bot/cli.py` | Subcommands below |
| Dashboard | `bot/dashboard/app.py` (FastAPI, optional) | Not hot path |
| Diagnostics | `bot/diagnostics/facade.py` | Re-export hub |

**CLI subcommands:** `run` (default), `harvest`, `status`, `stop`, `outcomes`, `backtest`, `replay`, `db migrate`, `db clean`. Doctor runs inside `run` startup (not a separate subcommand).

## Hard constraints (enforce on every edit)

1. No auto-trading logic
2. No authenticated Binance endpoints
3. No new test files or mock harnesses
4. All edits must pass: `python -m py_compile $(find bot -name "*.py")`
5. One commit per logical phase; message: `phase-<X>: <description>`

## SL Analysis (last updated 2026-06-05)

Overall SL rate before fixes: **100%** among executed exits (10/10; n=44 outcomes, see `REPORT_SL_ANALYSIS.md`).

Root causes confirmed: **Cause A only** (entry timing / late chase).

Fixes applied: **fix-sl-A** — confirmed-bar detection in `whale_walls`, `spread_strategy`, `btc_correlation`; `entry_staleness` filter (1.5×ATR%, default on).

Not applied (not confirmed): fix-sl-B (stop tight), fix-sl-C (regime), fix-sl-D (weak score), fix-sl-E (strategy bug).

Score floor: `min_score = 0.65` in `config.toml`. Regime filter: not added. Strategies disabled: none.

Next review: after 50+ new executed outcomes with fix-sl-A.

## Known architectural debt (do not silently work around — report and ask)

- **DUAL PERSISTENCE (resolved Phase E):** legacy `signals` / `outcomes` tables are now **READ-ONLY**. All runtime writes go to `active_signals` / `signal_outcomes`. Dashboard/analytics still reads legacy tables. Do not add writes to legacy tables. Planned: drop legacy tables in a future schema migration (Phase H, not yet started).
- **Phase G (tracking):** `tracking.py` split — lifecycle ~998 LOC; review in `_tracking_review.py` (~935); Telegram ids in `_tracking_telegram.py` (~103). Stats helpers (`_stats_snapshot`, `_record_setup_outcome`) stayed in `tracking.py` (<150 LOC, no `_tracking_stats.py`).
- **20 files remain above 1,000 LOC** (post-G). Largest: `bot/dashboard/app.py` (~1,779), `bot/market/ws.py` (~1,777). Runtime priorities: `symbol_analyzer.py` (~1,459), `delivery_orchestrator.py` (~1,323).
- **Phase F** decomposed `memory.py` / `symbol_analyzer.py` / `delivery_orchestrator.py` partially; all three remain above 1,000 LOC. Further extraction deferred.
- **`bot/runtime/scheduler.py`** (ex `bot/market/scheduler.py`) — kept; `bot/runtime/kline_handler.py` imports `analysis_intervals`. Do not delete.

## Top-level layout (phase-arch, 2026-06-10)

`engine/` = shared kernel (market, features, domain, errors/coercion/secrets/telegram/contract/telemetry/data_readiness) · `bot/` = main bot · `hunt/` = standalone hunter (`hunt_core`). Dependency: `engine ← bot` only; `hunt` is independent (CCXT-only market plane); `engine` must never import `bot.*`/`hunt`; `bot` must never import `hunt`.

## Strategy catalog

42 strategies via `STRATEGY_CLASSES` → `StrategyRegistry.register()`.
Enabled per strategy: `config.toml` `[setups.<id>]`.
Metadata: `engine/domain/strategy_catalog.py` (`CATALOG_ENTRIES`, 38 entries).
New strategy: detector file + `STRATEGY_CLASSES` + `CATALOG_ENTRIES` + config key + optional `config/strategies/<id>.toml`.

## Testing

- Live only: `PYTEST_LIVE=1 pytest tests/live/ -v` — needs Binance network
- Do **not** add new test files

## Environment

- Python **3.14.5** (venv); `requires-python >=3.14,<3.15` in `pyproject.toml`
- Install: `pip install -e ".[live,dev,test]"` or uv equivalent
- Run bot: `python main.py run` (or `python main.py`)
- Lint: `ruff check bot/`
- Typecheck: `mypy bot/` (strict with per-module overrides)
- Before live/smoke: `python scripts/clean_session_data.py --mode smoke --config config.toml`

## Related docs

- Human architecture: `ARCHITECTURE.md`
- Audit baseline: `AUDIT_REPORT.md`
- Operator playbook: `docs/SOLO_OPERATOR_PLAYBOOK.md`
- Backlog IDs only: `docs/DEFINITION_OF_DONE.md`

## graphify

This project has a knowledge graph at graphify-out/ with god nodes, community structure, and cross-file relationships.

Rules:
- For codebase questions, first run `graphify query "<question>"` when graphify-out/graph.json exists. Use `graphify path "<A>" "<B>"` for relationships and `graphify explain "<concept>"` for focused concepts. These return a scoped subgraph, usually much smaller than GRAPH_REPORT.md or raw grep output.
- If graphify-out/wiki/index.md exists, use it for broad navigation instead of raw source browsing.
- Read graphify-out/GRAPH_REPORT.md only for broad architecture review or when query/path/explain do not surface enough context.
- After modifying code, run `graphify update .` to keep the graph current (AST-only, no API cost).
