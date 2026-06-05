# CLAUDE.md — Project context for Claude Code agent

## What this project is

Crypto futures **signal-analytics** bot (no auto-trading, no authenticated Binance endpoints).
Signals only. Telegram delivery. Public Binance USDⓈ-M endpoints only.

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

## Frozen / immutable

- `bot/delivery/contract.py::validate_signal_contract()` — **DO NOT MODIFY**
- Strategy IDs in `bot/strategies/__init__.py::STRATEGY_CLASSES` — add ok; remove only with `CATALOG_ENTRIES` + config sync
- DB schema changes — **new steps in `bot/migrations.py` only**; never raw `ALTER` outside migrations

## Module ownership map

| Concern | Primary module | Notes |
|---------|----------------|-------|
| WS market data | `bot/market/ws.py` + `_ws_connection.py` + `_ws_parsers.py` | Public interface unchanged on `ws.py` |
| REST market data | `bot/market/rest_impl.py` + `_rest_circuit.py` + `_rest_frames.py` | `RestCircuitMixin` on `RestHttpMixin` |
| Feature pipeline | `bot/features/prepare.py` → `prepare_frame.py` | Polars hot path |
| Strategy execution | `bot/engine/engine.py` + `registry.py` | 38 strategies |
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
| Config | `bot/domain/config.py` + `config.toml` | `BotSettings` |
| Secrets | `bot/secrets.py` | Canonical: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` |
| CLI | `bot/cli.py` | Subcommands below |
| Dashboard | `bot/dashboard/app.py` (FastAPI, optional) | Not hot path |
| Diagnostics | `bot/diagnostics/facade.py` | Re-export hub |

**CLI subcommands:** `run` (default), `harvest`, `status`, `stop`, `outcomes`, `backtest`, `replay`, `db migrate`, `db clean`. Doctor runs inside `run` startup (not a separate subcommand).

## Hard constraints (enforce on every edit)

1. No auto-trading logic
2. No authenticated Binance endpoints
3. No new test files or mock harnesses
4. `validate_signal_contract()` is frozen
5. All edits must pass: `python -m py_compile $(find bot -name "*.py")`
6. One commit per logical phase; message: `phase-<X>: <description>`

## SL Analysis (last updated 2026-06-05)

Overall SL rate before fixes: **100%** among executed exits (10/10; n=44 outcomes, see `REPORT_SL_ANALYSIS.md`).

Root causes confirmed: **Cause A only** (entry timing / late chase).

Fixes applied: **fix-sl-A** — confirmed-bar detection in `whale_walls`, `spread_strategy`, `btc_correlation`; `entry_staleness` filter (1.5×ATR%, default on).

Not applied (not confirmed): fix-sl-B (stop tight), fix-sl-C (regime), fix-sl-D (weak score), fix-sl-E (strategy bug).

Score floor: unchanged (`min_score` 0.53 in `config.toml`). Regime filter: not added. Strategies disabled: none.

Next review: after 50+ new executed outcomes with fix-sl-A.

## Known architectural debt (do not silently work around — report and ask)

- **DUAL PERSISTENCE (resolved Phase E):** legacy `signals` / `outcomes` tables are now **READ-ONLY**. All runtime writes go to `active_signals` / `signal_outcomes`. Dashboard/analytics still reads legacy tables. Do not add writes to legacy tables. Planned: drop legacy tables in a future schema migration (Phase H, not yet started).
- **Phase G (tracking):** `tracking.py` split — lifecycle ~998 LOC; review in `_tracking_review.py` (~935); Telegram ids in `_tracking_telegram.py` (~103). Stats helpers (`_stats_snapshot`, `_record_setup_outcome`) stayed in `tracking.py` (<150 LOC, no `_tracking_stats.py`).
- **20 files remain above 1,000 LOC** (post-G). Largest: `bot/dashboard/app.py` (~1,779), `bot/market/ws.py` (~1,777). Runtime priorities: `symbol_analyzer.py` (~1,459), `delivery_orchestrator.py` (~1,323).
- **Phase F** decomposed `memory.py` / `symbol_analyzer.py` / `delivery_orchestrator.py` partially; all three remain above 1,000 LOC. Further extraction deferred.
- **`bot/market/scheduler.py`** — kept; `bot/runtime/kline_handler.py` imports `analysis_intervals`. Do not delete.

## Strategy catalog

38 strategies via `STRATEGY_CLASSES` → `StrategyRegistry.register()`.
Enabled per strategy: `config.toml` `[setups.<id>]`.
Metadata: `bot/domain/strategy_catalog.py` (`CATALOG_ENTRIES`, 38 entries).
New strategy: detector file + `STRATEGY_CLASSES` + `CATALOG_ENTRIES` + config key + optional `config/strategies/<id>.toml`.

## Testing

- Offline waves: `pytest tests/test_wave_f*.py tests/test_wave_i_calibration.py -q`
- Live: `tests/live/` (`PYTEST_LIVE=1`) — needs network
- Do **not** add new test files; fix existing tests if a refactor breaks them

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
