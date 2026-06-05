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
| Persistence CRUD | `bot/persistence/repository/memory.py` | SQLite + parquet |
| Signal lifecycle | `bot/persistence/tracking.py` | `active_signals` / `signal_outcomes` |
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

## Known architectural debt (do not silently work around — report and ask)

- **Dual persistence:** `signals` / `outcomes` (legacy, read-only after Phase E) vs `active_signals` / `signal_outcomes` (primary). Do not add new dual-writes.
- **22 files still >1,000 LOC** (e.g. `memory.py`, `symbol_analyzer.py`, `delivery_orchestrator.py`). Further decomposition = Phase F.
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

- Python **3.14** (`requires-python >=3.14,<3.15` in `pyproject.toml`)
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
