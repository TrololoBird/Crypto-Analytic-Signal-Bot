# Architecture

## Runtime flow

1. `main.py` launches CLI.
2. `bot.cli.run()` constructs `SignalBot`.
3. `SignalBot` orchestrates market data, strategy evaluation, confluence scoring, delivery, and tracking.

## Main modules

- `bot/application/`: runtime orchestration entry (`bot.py`) plus focused runtime components:
  - `kline_handler.py` — kline-close orchestration + per-symbol select/deliver.
  - `intra_candle_scanner.py` — throttled bookTicker intra-candle trigger path.
  - `fallback_runner.py` — tracking review + emergency fallback loops.
  - `oi_refresh_runner.py` — periodic OI/L/S cache warmup batches.
  - `telemetry_manager.py` — telemetry row construction/emission.
  - `symbol_analyzer.py`, `delivery_orchestrator.py`, `market_context_updater.py`, etc.
- `bot/core/`: engine, event bus, memory repository, diagnostics.
- `bot/strategies/`: setup detectors registered in the modern strategy registry.
- `bot/setups/`: shared setup helpers and metadata.
- `bot/market_regime.py` + `bot/regime/`: market regime analyzers.
- `bot/confluence.py`: score blending and optional ML adjustment.
- `bot/learning/`: walk-forward optimization, regime-aware param bounds, and outcome store adapters.
- `bot/websocket/`: extracted WebSocket helper submodules (e.g., enrichment math in `enrichment.py`).
- `bot/features_microstructure.py`: isolated microstructure feature builder used by `bot/features.py`.
- `bot/features_structure.py`: extracted structure/indicator helpers (Ichimoku/WMA/HMA) used by `bot/features.py`.

## Data and state

- Config sources: `config.toml`, `config.toml.example`.
- Persistence: `MemoryRepository` (SQLite-backed).
- Telemetry: JSONL appenders in telemetry directory.

## Market-data contract

- Runtime market data is restricted to public Binance USDⓈ-M endpoints.
- REST is served through the public endpoint registry in `bot/market_data.py`; private/auth/signed routes and non-USDⓈ-M hosts (including `eapi.binance.com`) are rejected at validation time.
- WebSocket routing is split intentionally:
  - `/public` for `@bookTicker`
  - `/market` for `@kline_*`, `@aggTrade`, `!ticker@arr`, `@markPrice`, `!forceOrder@arr`
- Shortlist maintenance is two-speed:
  - `rest_full`: infrequent full rebalance from exchange metadata + `ticker/24hr`
  - `ws_light`: frequent rerank from cached public WS ticker context plus cached public derivatives metrics
- Shortlist enrichment pulls freshness only from public WS caches:
  - `ticker_age_seconds`
  - `mark_price_age_seconds`
  - `book_age_seconds` from bookTicker cache age

## Signal-context contract

- Crowd positioning is family-aware: continuation/breakout setups consume crowd support/headwind differently from reversal setups.
- Missing or stale crowd context should degrade to neutral handling, not hard dependency, unless fast-context strictness already applies.
- Structural short stops anchor to resistance above entry rather than to the nearest arbitrary level below entry.

## Design boundaries

- Strategy logic should stay in strategies/setup helpers, not in orchestration.
- Persistence goes through repository abstractions.
- Async I/O boundaries are preserved in runtime modules.
- `SignalBot` should delegate long-running loops and event-path specifics to application components rather than re-introducing inline monolithic handlers.
