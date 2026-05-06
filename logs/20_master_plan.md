# Phase 2 Master Plan

Generated: 2026-05-05T02:35:31+00:00

## Vision

A signal-only Binance USD-M public-data bot that produces transparent Telegram alerts, tracks every signal to closure, and uses outcome evidence to tune strategy gates without hiding uncertainty.

## Architecture

- Runtime: `main.py` -> `bot.cli.run()` -> `SignalBot`.
- State: SQLite via `MemoryRepository`; active lifecycle in `active_signals`, closed performance in `signal_outcomes`.
- Data flow: REST/WS market data -> Polars feature preparation -> strategy engine -> family/context filters -> global filters -> Telegram delivery -> tracking/outcome persistence -> analytics/dashboard.

## Data Acquisition

- Keep Binance USD-M public endpoints only.
- Preserve `rest_full` versus `ws_light` shortlist paths and keep fallback telemetry explicit.
- Keep WS kline scope narrow and use cached REST context frames for 5m/1h/4h until live evidence proves broader WS subscriptions are stable.

## Indicator Suite

- Keep Polars-native feature work.
- Treat derivative context missingness as data quality, not strategy failure.
- Add backtest/optimizer tooling before adding new indicator libraries or new nonlinear gates.

## Strategy Framework

- Registered/current strategy count target: 37.
- Use setup-score adjustments as score modifiers, not hard suppressors.
- For high-SL setups with enough outcomes, tighten entry evidence and widen noise buffers conservatively.
- For zero-hit setups, first separate missing-data causes from truly over-strict thresholds.

## Signal Lifecycle

- Every delivered signal gets a stable tracking ID/ref, Telegram message ID, active state row, and eventual `signal_outcomes` row.
- Stale active signals must be reviewed at startup and on periodic tracking sweeps.
- Superseded/expired/smart-exit outcomes remain visible in analytics but should not be counted as simple wins unless profitable.

## Telegram Channel

- Primary value is the signal card and tracking updates.
- Keep message IDs persisted for edits/replies.
- Pace sends locally to avoid flood-control retries during bursts.
- Keep audit batches concise and suppress duplicates.

## Dashboard

- Keep `/api/status`, `/api/signals/active`, `/api/signals/recent`, `/api/analytics/report`, and `/api/strategies` live.
- Strategy list must show all 37 strategy IDs and enabled state from config.
- Performance panels should use `signal_outcomes`, not empty legacy `signals`/`outcomes` tables.

## Scaling Plan

- Prefer bounded concurrency and cached public REST context over expanding WS streams.
- Increase shortlist size only after WS buffer drops and REST timeout rates stay low in a live run.
- Rate-limit Telegram and batch non-critical audit updates.

## Technology Stack

- Python 3.13, asyncio, Polars, FastAPI/uvicorn for dashboard, SQLite/aiosqlite for persistence, aiogram for Telegram.
- No signed Binance endpoints, no account APIs, no user-data streams, no auto-trading.

## Executed Task Plan

| Task | Files | Change | Success Criteria | Verification |
| --- | --- | --- | --- | --- |
| TASK_001 | `scripts/phase0_forensics.py`, `logs/00_file_map.md`..`04_telemetry_audit.md` | Refresh Phase 0 forensic reports | Reports regenerated from current filesystem/DB/logs/telemetry | `python scripts/phase0_forensics.py` |
| TASK_002 | `bot/application/symbol_analyzer.py`, `tests/test_symbol_analyzer_telemetry.py` | Apply setup performance adjustment to score instead of hard-suppressing all negative setups | Mild -0.05 penalty keeps signal eligible for global filters | targeted pytest |
| TASK_003 | `config.toml`, `config.toml.example` | Tighten high-SL setup parameters and align explicit dashboard/tracking config | Config validates; example stays aligned | `python -m scripts.validate_config` |
| TASK_004 | `bot/messaging.py` | Add local Telegram pacing before sends/edits/photos | Reduced flood-control risk under bursts | import/tests/live smoke |
| TASK_005 | `logs/10_analysis.md`, `logs/20_master_plan.md` | Generate evidence-backed analysis and plan | Reports exist and cite confirmed/uncertain evidence | `python scripts/phase1_analysis.py` |
