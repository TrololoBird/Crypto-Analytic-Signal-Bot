# Architecture reference — Crypto Signal Bot v9

Human-facing architecture reference. AI agents should prefer `CLAUDE.md` at session start.

## Layer diagram

```text
[Binance WS]  → ws.py (+ _ws_connection, _ws_parsers)
                    ↓
              EventBus (bot/core/event_bus.py)
                    ↓
         SymbolAnalyzer (bot/runtime/symbol_analyzer.py)
                    ↓
         SignalEngine (bot/engine/engine.py) ← 38 strategies (STRATEGY_CLASSES)
                    ↑
[Binance REST] → rest_impl.py (+ _rest_circuit, _rest_frames)
                    ↓
              data.py (facade) → frames + enrichments
                    ↓
         features/prepare.py + features/prepare_frame.py (Polars)
                    ↓
         DeliveryOrchestrator (bot/runtime/delivery_orchestrator.py)
                    ↓
         validate_signal_contract()  [FROZEN — bot/delivery/contract.py]
                    ↓
         filters.py → confluence.py (3-of-5) → deliver.py
                    ↓
         Telegram (aiogram) + optional dashboard (FastAPI)
                    ↓
         tracking.py (lifecycle) + repository/memory.py (SQLite)
```

## Key invariants

1. **Delivery gate order:** Every outbound trade plan passes `validate_signal_contract()` → hard confluence gate → `delivery.deliver()` — no bypass.
2. **Public market data only:** No signed REST, no user streams, no order placement.
3. **Event-driven primary path:** Kline-close (and related WS events) drive analysis; emergency REST fallback is secondary.
4. **Immutable signal contract:** `Signal` schema and contract validation define what may be sent; frozen validator.
5. **Single migration authority:** `bot/migrations.py` owns `schema_version` writes; repository bootstrap creates tables but does not version-stamp.

## Data flow: signal lifecycle

1. **Market event:** Binance WS emits kline close (or book/aggTrade for fast-path tracking).
2. **EventBus:** `KlineCloseEvent` dispatched to `SignalBot._on_kline_close` → `CycleRunner` / `SymbolAnalyzer`.
3. **Frames:** REST/WS caches merged; `prepare_symbol()` builds multi-TF Polars frames on `PreparedSymbol`.
4. **Detection:** `SignalEngine` runs enabled strategies from `StrategyRegistry`; candidates become `Signal` objects with scores.
5. **Pre-delivery:** `DeliveryOrchestrator` ranks/filters; family/MTF gates in `SymbolAnalyzer` where applicable.
6. **Contract + confluence:** `validate_signal_contract()` then `ConfluenceEngine` (3-of-5 pillars).
7. **Delivery:** `deliver()` → Telegram HTML; public audit CSV optional.
8. **Tracking:** `SignalTracker` arms `active_signals` rows, updates on activation/TP/SL, closes to `signal_outcomes`.
9. **Telemetry:** JSONL under `data/bot/telemetry/` per run; not the same as SQLite lifecycle tables.

Legacy `signals` / `outcomes` tables may still be **read** for analytics/dashboard; **writes removed in Phase E**.

## Configuration layers

1. **`config.toml`** — runtime knobs (filters, universe, WS, delivery caps, enabled setups).
2. **`config/strategies/*.toml`** + **`config_strategies.toml`** — per-strategy parameter overrides.
3. **`.env`** — secrets only (`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, optional `GITHUB_TOKEN`, operator IDs). Legacy aliases still accepted in `bot/secrets.py`.
4. **`bot/domain/config.py`** — Pydantic merge into `BotSettings` via `load_settings()`.

## Known debt

- **Dual persistence:** `signals` / `outcomes` (legacy, read-only after Phase E) vs `active_signals` / `signal_outcomes` (primary). Do not add new dual-writes.
- **22 files still >1,000 LOC** (e.g. `memory.py`, `symbol_analyzer.py`, `delivery_orchestrator.py`). Further decomposition = Phase F.
- **`bot/market/scheduler.py`** — kept; `bot/runtime/kline_handler.py` imports `analysis_intervals`. Do not delete.
