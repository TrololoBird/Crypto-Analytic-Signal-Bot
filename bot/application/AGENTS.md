# Application Agent

## Scope

Work in `bot/application/`, currently centered on `bot.py`.

## Role

`SignalBot` is the runtime orchestrator. It wires together market data, EventBus, strategy execution, delivery, tracking, telemetry, and shutdown behavior.

## Read Before Editing

- The touched section in `bot/application/bot.py`
- Relevant event definitions in `bot/domain/events.py`
- Event dispatch in `bot/core/event_bus.py`
- Related integration modules found by `rg`: usually `bot/ws_manager.py`, `bot/market_data.py`, `bot/tracking.py`, `bot/delivery.py`, `bot/core/memory/repository.py`

## Local Rules

- Keep orchestration logic centralized here; do not duplicate runtime control flow into strategies or helpers.
- Respect startup, task lifecycle, cancellation, and graceful shutdown behavior.
- Preserve async boundaries and concurrency limits derived from settings.
- If a handler changes because an event changed, update the producer/consumer chain in the same change.
- If persistence flows change, verify repository method contracts instead of assuming payload shapes.
- Preserve the current stdlib `logging` style in this module unless the task explicitly refactors logging.
- Strategy runtime coverage belongs here: if a detector needs public enrichment,
  verify that `SymbolAnalyzer`, `FuturesWSManager`, `MarketContextUpdater`, or
  OI/funding runners actually populate the `PreparedSymbol` field.
- Do not solve a strategy no-signal issue by changing enabled flags. Surface
  missing data as structured skip/reject telemetry and repair the producer path.

## Token Discipline

- Read only the initializer, handler, or helper sections relevant to the task; `bot.py` is large.
- Trace exact call paths with `rg` before opening additional modules.

## Verification

- Prefer event-flow tracing, compile/import checks, telemetry replay, and
  read-only live diagnostics for touched runtime behavior.
- Generated tests are not proof of live behavior; use them only as supplemental
  safety checks when explicitly useful.
