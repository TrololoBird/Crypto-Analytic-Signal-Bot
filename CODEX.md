# Codex Runtime Brief

This file is a current execution brief for agents. Historical audit plans were
removed from this file because they caused agents to re-run stale workflows.

## Start Here

1. Read `AGENTS.md`.
2. Read `docs/AGENT_PLAYBOOK.md`.
3. Read the nearest nested `AGENTS.md` for the module you are touching.
4. Verify the current code path with `rg` before trusting any older report.

## Non-Negotiables

- Do not use generated tests as proof that a strategy works.
- Do not disable strategies because of `experimental` or `beta` metadata.
- Do not patch symptoms before finding the data/contract/gate root cause.
- Do not add Binance private/auth/account/order/user-data endpoints.
- Do not fake missing market data with misleading proxy names.
- Do not delete files without proving they are unused or misleading.

## Strategy Work

All strategies exported in `bot/strategies/__init__.py::STRATEGY_CLASSES` must
be treated as live obligations. A strategy can return no signal only when it
emits a precise, inspectable reason through `StrategyDecision` or rejection
telemetry.

When a strategy does not signal:

1. Confirm it is exported, registered, enabled, and config-backed.
2. Inspect required frame columns and `PreparedSymbol` enrichment fields.
3. Trace missing data to REST, WS, context updater, OI/funding runner, or
   `SymbolAnalyzer.ws_cache_enrichments`.
4. Inspect global filter and confluence gates.
5. Calibrate named config thresholds or rewrite detector logic from a real
   public-data-compatible strategy reference.
6. Re-run `scripts/live_check_strategies.py` or an equivalent read-only replay
   and compare detector hits, reject reasons, and `strategy_errors=[]`.

`5m` and `4h` frames are contextual in `PreparedSymbol`; do not let a short
optional frame block 15m/1h strategies before their own guards run.

## Verification

Use source tracing, compile/import checks, config diagnostics, telemetry reason
counts, and read-only live scripts. Use tests only as supplemental smoke checks
when explicitly useful.
