# Crypto Signal Bot v9 — Claude Code Reference

## What this is

Event-driven Binance USDⓈ-M public signal bot: WS/REST → Polars features → 38 strategies → contract + confluence gates → Telegram trade plans.
No auto-trading, no private Binance APIs, no user registration.

## Entry points

```bash
python main.py run
python main.py harvest --minutes 120
python main.py status | stop | doctor
make check
make smoke
make live-smoke
make validate-config
python scripts/clean_session_data.py --mode smoke --config config.toml
python scripts/validate_config.py --config config.toml
```

## Architecture

- `bot/domain/` — `BotSettings`, schemas, strategy catalog, contracts
- `bot/market/` — REST `rest_impl.py`, WS `ws.py`, universe, enrichment, proxy
- `bot/features/` — `prepare.py`, `prepare_frame.py`, Wilder ATR/RSI indicators
- `bot/engine/` — `SignalEngine`, `StrategyRegistry`, lanes
- `bot/strategies/` — 38 `*Setup` classes, `_common.py`, `_roadmap.py`
- `bot/setups/` — `base`, `spec_runtime`, `smc` (no `detectors/`)
- `bot/runtime/` — `bot.py`, `cycle_runner.py`, `symbol_analyzer.py`, `delivery_orchestrator.py`
- `bot/delivery/` — contract, confluence, filters, deliver, telegram, tiers
- `bot/persistence/` — SQLite tracking, outcomes, `memory.py`
- `bot/diagnostics/` — telemetry, session ops
- `bot/dashboard/` — optional FastAPI UI (not hot path)

## Signal pipeline

```text
bot/market/ws.py
bot/runtime/cycle_runner.py
bot/runtime/symbol_analyzer.py
bot/engine/engine.py
bot/delivery/filters.py
bot/delivery/confluence.py
bot/runtime/delivery_orchestrator.py
bot/delivery/telegram_routing.py
```

Delivery invariant: `validate_signal_contract` → `_hard_confluence_gate` (3-of-5) → `delivery.deliver`.

## Key types

From `bot/domain/schemas.py`:

- `UniverseSymbol` — shortlist row, liquidity, `strategy_fits`
- `SymbolFrames` — raw OHLCV 5m/15m/1h/4h + bid/ask
- `PreparedSymbol` — post-`prepare_symbol` work frames + enrichments
- `Signal` — immutable trade plan: entries, SL, TPs, score, `setup_id`
- `PipelineResult` — per-cycle funnel: candidates, rejects, status

## Constraints

1. Never place orders, use trading APIs, or add account authentication.
2. Never bypass `validate_signal_contract` → hard confluence gate → `delivery.deliver`.
3. Never import forbidden legacy paths (`bot/application/`, `bot/setups/detectors/`, ccxt).
4. Never use `shift(-N)` or pandas on live signal paths.
5. Never put LLM inference on the hot path.
6. Never create ABC/Protocol/factory layers without architect approval.
7. Never split modules under 500 LOC or frozen monoliths without explicit request.
8. Never generate test files unless explicitly requested.
9. Never modify `bot/static/`.
10. Never disable strategies silently — fix detectors or document config reason.
11. Wilder ATR/RSI; Bollinger std `ddof=1`.
12. Agent executes all commands and probes — no runbooks for the human.
13. Before live/smoke: `clean_session_data.py --mode smoke`.
14. Backlog: only `docs/DEFINITION_OF_DONE.md` IDs (OPS-*, OPT-*).

## Known debt

| Severity | file:line | description |
|----------|-----------|-------------|
| OPS | `config.toml` | `action_min_score=0.72` vs live max confluence ~0.68 — weighted bridge armed, inactive until scores rise |

## How to add a strategy

1. Catalog entry in `bot/domain/strategy_catalog.py`.
2. Defaults in `config.toml`; run `make validate-config`.
3. Detector in `bot/strategies/<name>.py`; helpers from `_common.py` / `_roadmap.py`.
4. Subclass `RoadmapSetup` or `SpecDetectorSetup`; implement `detect()` or `spec_detect`.
5. Export in `STRATEGY_CLASSES` in `bot/strategies/__init__.py`; run `make check`.

## Test commands

```bash
make check
make smoke
make live-smoke
make validate-config
python scripts/check_circular_imports.py
pytest tests/test_wave_f9_agent_*.py tests/test_wave_f10_agent_*.py tests/test_wave_i_calibration.py -q
PYTEST_LIVE=1 pytest tests/live/ -v
```

Session start: read `HANDOFF_REPORT.md`. Agents: `.claude/agents/`. Rules: `.claude/rules/`.
