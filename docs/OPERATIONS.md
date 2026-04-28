# Operations

## Dashboard

The bot includes a built-in web dashboard for real-time monitoring.

- **URL**: http://localhost:8080 (default)
- **Auto-open**: Enabled by default (`auto_open_dashboard = true` in config)
- **Port**: Configurable via `dashboard_port` in `[bot.runtime]` section

### Dashboard Features

- **Overview Tab**: Bot status, shortlist size, open signals, WS latency, market regime
- **Signals Tab**: Active signals with entry/stop/TP levels, scores, and risk/reward ratios
- **Analytics Tab**: Performance metrics (total signals, win rate, avg R/R)
- **Settings Tab**: Strategy configuration and enabled status

### Keyboard Shortcuts

- `1-4` — Switch between tabs (Overview, Signals, Analytics, Settings)
- `R` — Refresh data manually
- `?` — Show keyboard shortcuts help

### API Endpoints

- `GET /api/status` — Bot runtime status
- `GET /api/signals/active` — Active trading signals
- `GET /api/health` — Health check with WS connection status
- `GET /api/strategies` — Strategy list with enabled status
- `GET /api/metrics` — Detailed metrics
- `GET /api/analytics/report?days=30` — Performance analytics

## Local commands

- `make check` — full local checks.
- `make lint` — lint and type checks.
- `make test` — run tests.
- `make run` — start bot runtime.
- `make dry-run` — run without sending live deliveries.
- Targeted remediation suites (`test_regression_suite_*` naming):
  - `pytest -q tests/test_regression_suite_remediation_intra_candle.py`
  - `pytest -q tests/test_regression_suite_remediation_indicators.py`
  - `pytest -q tests/test_regression_suite_tracking_delivery.py`
  - `pytest -q tests/test_regression_suite_strategies.py`
  - `pytest -q tests/test_regression_suite_contracts.py`
  - `pytest -q tests/test_regression_suite_engine.py`
- Fast triage by suite markers:
  - `pytest -q -m regression_remediation`
  - `pytest -q -m regression_remediation_runtime`
  - `pytest -q -m regression_remediation_indicators`

## Recommended routine

1. Validate config values in `config.toml`.
2. Run `make check`.
3. Start with `make dry-run`.
4. Inspect telemetry logs (`telemetry/`) and repository state.
5. Enable live mode only after dry-run sanity checks.

## Runtime health signals

- heartbeat metrics (health manager)
- tracking queue/drain status
- memory repository summary
- data quality and strategy decision JSONL traces
- emergency fallback checks (`fallback_checks.jsonl`)
- cycle/symbol telemetry emitted via `TelemetryManager`
- shortlist telemetry distinguishes `rest_full`, `ws_light`, `cached`, and `pinned_fallback` sources and includes top composite-score reasons
- shortlist telemetry also records `source_before`/`source_after`, `fallback_reason`, and cached metadata (`cached_shortlist_age_s`, `cached_shortlist_size`) for each refresh decision

### Shortlist fallback telemetry interpretation

- `fallback_reason` is normalized to: `ws_cache_cold`, `full_refresh_due`, `refresh_exception`, `live_empty`, `using_cached`, `using_pinned`, or `unknown`.
- `source_before`/`source_after` capture the transition of shortlist source for each cycle (for example `ws_light -> cached`).
- `cached_shortlist_age_s` and `cached_shortlist_size` are populated when `source_after="cached"`; otherwise they stay `null`.
- `decision-state` (`full_refresh_due`, `ws_cache_warm`, `has_symbol_meta`) is logged before branching and should be correlated with `fallback_reason` during incident review.

## Binance boundary checks

- Keep runtime on public USDⓈ-M market-data endpoints only.
- Runtime boundary explicitly denies non-USDⓈ-M Binance REST hosts (for example `eapi.binance.com`), even when endpoints are public.
- `bot.intelligence.allow_runtime_options_eapi` must remain `false` for production runtime; enabling it is an explicit non-default exception and must be treated as a boundary override.
- Treat any `/private`, signed/auth route, `listenKey`, or user-data stream as a configuration/code regression.
- After market-data changes, rerun endpoint grep plus the contract-focused suites (`tests/test_regression_suite_contracts.py`, `tests/test_regression_suite_tracking_delivery.py`) to confirm public-only guardrails still hold.

## ML training quality gates

- Use `python -m bot.ml.train` with `--report` to persist machine-readable walk-forward output.
- `summary` metrics are aggregated with `test_rows` weights, so larger validation windows have proportionally larger impact.
- Use gates for CI/non-interactive validation:
  - `--min-windows` (expected minimum number of evaluated windows),
  - `--min-accuracy`, `--min-precision`, `--min-recall`, `--min-f1` (all bounded in `[0.0, 1.0]`).
- CLI exits with code `2` when any enabled gate fails and includes failure reasons in `quality_gate.failures`.
- Runtime ML integration should import `MLFilter` from `bot.ml` (canonical path). The top-level module `bot.ml_filter` is kept only as a backward-compatible shim.
- Live-guardrail decision is centralized on runtime paths via `SignalClassifier.runtime_guardrail_decision(...)` (which delegates to `bot.ml.guardrails.evaluate_live_model_guardrail`), so baseline kinds are blocked only in live mode (`is_live && model_kind in {centroid_baseline, linear_baseline} => disable`).
- Guardrail telemetry is emitted as structured fields in logs: `stage`, `model_kind`, `disable_reason`, `is_live`, `count` (including orchestrator init status row).

## Incident checklist

1. Verify exchange connectivity and WS status.
2. Confirm fresh klines/market context.
3. Inspect reject reasons in telemetry.
   - For ML guardrails, filter `ML guardrail` / `ML runtime status` events and verify: `model_kind`, `disable_reason`, `stage`, `is_live`.
4. Validate cooldown/blacklist status in repository.
5. Restart only after root cause is identified.

## PR doc-change gate

- CI now enforces a docs-parity check for pull requests that touch critical runtime paths: `main.py`, `bot/application`, `bot/core`, `bot/tasks`, `bot/telegram`, `bot/websocket`, `bot/features*`, `bot/ml*`, `bot/market_data.py`, `bot/ws_manager.py`, `bot/config.py`.
- If any of those paths change, at least one file under `docs/` must also change in the same PR.
- The same expectation is reflected in the pull-request checklist template.
