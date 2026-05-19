# Agent Playbook

This is the compressed map agents should read after `AGENTS.md`. It exists to
avoid re-learning the repository from stale audits every session.

## Runtime Map

1. `main.py` calls `bot.cli.run()`.
2. `bot.cli._main()` loads `config.toml` through `bot/domain/config.py`.
3. `bot/application/bot.py::SignalBot` wires market data, WS streams,
   shortlist refresh, strategy registry, telemetry, tracking, and delivery.
4. `bot/features.py::prepare_symbol()` converts OHLCV frames into
   `PreparedSymbol`.
5. `bot/core/engine/engine.py::SignalEngine` runs enabled strategies from
   `bot/strategies/__init__.py::STRATEGY_CLASSES`.
6. `bot/application/symbol_analyzer.py` applies family checks, filters,
   confluence, telemetry, and candidate selection.
7. Delivery goes through `bot/application/delivery_orchestrator.py` and the
   Telegram infrastructure.

## Strategy Rules

- All 38 registered setup ids are expected to stay enabled unless the strategy
  is intentionally removed from `STRATEGY_CLASSES`.
- Metadata status (`experimental`, `beta`, `production`) is descriptive only.
  It is not a reason to disable a detector.
- A detector that does not signal needs root-cause analysis:
  missing source data, stale enrichment, insufficient history, missing feature,
  global filter gate, threshold calibration, context conflict, implementation
  bug, or normal market condition.
- `trend_conflict_1h` is family-aware. Trend-following/continuation/breakout
  setups are hard-rejected when they fight confirmed 1h context. Countertrend
  exhaustion setups (`reversal`, `orderflow`, `liquidity`, `sentiment`) should
  be penalized and scored, not suppressed before confluence.
- Do not fake missing market data. If a strategy name implies liquidations,
  consume `forceOrder`-derived liquidation context. If it implies spread
  arbitrage, populate and use the actual spread field or rename/rewrite the
  strategy.
- Strategy thresholds belong in `[bot.filters.setups]`; enable flags belong in
  `[bot.setups]`.

## Data Rules

- Binance scope is public USDⓈ-M market data only.
- Allowed REST/WS surfaces are market data, klines, depth/book ticker,
  aggregate trades, mark price/funding, open interest, long/short ratios, taker
  ratios, basis, and force-order liquidation streams.
- Never add signed endpoints, account/trade endpoints, `listenKey`, API keys, or
  user-data streams.
- For time-sensitive API claims, verify official Binance docs and, where useful,
  live read-only calls.
- Feature work must use the installed Polars stack from `requirements.txt`.
  `polars_ta` is enabled when importable, but outputs must be checked against
  the bot's feature contract before changing strategy thresholds.

## Verification Rules

- Generated tests are not proof of strategy behavior or trading edge.
- First-line checks are:
  - `rg` call-path review
  - `python -m compileall bot`
  - config/strategy export diagnostics
  - telemetry JSONL reason counts
  - read-only live scripts or small local diagnostic scripts
  - official docs and real GitHub implementations for strategy/data concepts
- If tests are present, treat them as supplemental smoke indicators only.

## Dead Code Rules

- Delete code, docs, scripts, or tests only after proving they are unused or
  misleading with `rg`, import/call-path review, config references, and runtime
  artifacts.
- Do not delete a module only because it has weak tests or stale docs.
- If a stale audit/doc is retained for history, label it historical and point to
  the current source of truth.

## Current Source Of Truth

- Strategy surface: `bot/strategies/__init__.py`, `[bot.setups]`,
  `[bot.filters.setups]`, `docs/STRATEGIES.md`
- Live strategy health is checked with `scripts/live_check_strategies.py`.
  A strategy with zero delivered historical outcomes is not automatically dead:
  first compare detector hits, reject reasons, active/pending signals, and
  `signal_outcomes` reconciliation.
- `signal_outcomes` trade stats must exclude `expired_pending`,
  `unactivated_close`, and `superseded`; those are monitoring/delivery outcomes,
  not activated trades.
- Dashboard analytics defaults to current-run scope. Do not mix stale
  `signal_outcomes` from previous code with detector telemetry from the current
  process when deciding whether a strategy is broken. Compare three separate
  stages: detector hits from `strategy_decisions.jsonl`, selected/delivered
  signals from `selected.jsonl`/`delivery.jsonl`, and activated trade outcomes
  from SQLite.
- After large strategy/filter/tracking changes, the already-running bot process
  must be restarted before judging new behavior. Existing telemetry is useful
  for diagnosis, but it is not evidence about code that was changed after the
  process started.
- Runtime feature contract: `bot/domain/schemas.py`, `bot/features.py`,
  `docs/FEATURES_IO_CONTRACT.md`
- Binance boundary: `bot/market_data.py`, `bot/ws_manager.py`,
  `bot/websocket/`, `reports/binance_endpoint_registry.md`
- Operations: `docs/OPERATIONS.md`
- Runtime logging policy: expected throttling/fallback is `info/debug`;
  actionable failures are `error/exception/critical`. Do not reintroduce
  `warning`-level console noise as a way to avoid fixing or classifying runtime
  problems.
- Telegram signal delivery is signal-first: main messages should stay compact
  limit plans with entry range, one stop, TP1/TP2, RR, TTL, and optional BTC risk
  note. Longer analytics belongs in telemetry/docs or the disabled companion
  message path.
