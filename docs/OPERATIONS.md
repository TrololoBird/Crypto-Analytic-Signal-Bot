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
- Targeted remediation suites:
  - `pytest -q tests/test_remediation_intra_candle.py`
  - `pytest -q tests/test_remediation_indicators.py`
  - `pytest -q tests/test_remediation_regressions.py`

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

## Binance boundary checks

- Keep runtime on public USDⓈ-M market-data endpoints only.
- Treat any `/private`, signed/auth route, `listenKey`, or user-data stream as a configuration/code regression.
- After market-data changes, rerun the endpoint grep plus `tests/test_remediation_regressions.py` to confirm the public-only guardrails still hold.

## ML training quality gates

- Use `python -m bot.ml.train` with `--report` to persist machine-readable walk-forward output.
- `summary` metrics are aggregated with `test_rows` weights, so larger validation windows have proportionally larger impact.
- Use gates for CI/non-interactive validation:
  - `--min-windows` (expected minimum number of evaluated windows),
  - `--min-accuracy`, `--min-precision`, `--min-recall`, `--min-f1` (all bounded in `[0.0, 1.0]`).
- CLI exits with code `2` when any enabled gate fails and includes failure reasons in `quality_gate.failures`.
- Runtime ML integration should import `MLFilter` from `bot.ml` (canonical path). The top-level module `bot.ml_filter` is kept only as a backward-compatible shim.
- Live-guardrail decision is centralized via `bot.ml.guardrails.evaluate_live_model_guardrail` (`is_live && model_kind in {centroid_baseline, linear_baseline} => disable`).
- Guardrail telemetry is emitted as structured fields in logs: `stage`, `model_kind`, `disable_reason`, `count` (including orchestrator init status row).

## Incident checklist

1. Verify exchange connectivity and WS status.
2. Confirm fresh klines/market context.
3. Inspect reject reasons in telemetry.
4. Validate cooldown/blacklist status in repository.
5. Restart only after root cause is identified.
