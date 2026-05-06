# Phase 4 Validation

Generated: 2026-05-05T02:23:42Z

## Evidence Boundary

- Confirmed: validation commands below were run locally in `C:\Users\undea\Documents\bot2`.
- Confirmed: Binance validation used public USD-M websocket/REST data through the project live smoke harness.
- Confirmed limitation: the live smoke harness uses `FakeBroadcaster`; no real Telegram channel message was sent, edited, or verified.
- Unverified: production dashboard served over HTTP during the live run, real Telegram delivery, 2-hour live run, and 6-hour stability run.

## Static and Regression Checks

| Check | Result | Evidence |
| --- | --- | --- |
| Config validation | PASS | `python -m scripts.validate_config` returned `All checks passed` |
| Focused regression suite | PASS | `python -m pytest tests\test_cycle_runner_regressions.py tests\test_symbol_analyzer_telemetry.py tests\test_outcome_dashboard_regressions.py tests\test_runtime_endpoint_policy.py tests\test_market_data_limits.py -q` returned `19 passed` |
| Dashboard strategy cache probe | PASS | 37 strategy classes, 37 dashboard cache entries, 37 enabled config IDs |

## RUN_1: 30-Minute Live Smoke

Command:

```powershell
python scripts\live_smoke_bot.py --warmup-seconds 1800
```

Status: PARTIAL PASS

Confirmed:

- Process ran from 2026-05-05 04:49:19 to 2026-05-05 05:20:42 local time.
- Severity ERROR lines: 0.
- Severity WARNING lines: 0.
- Tracebacks: 0.
- RuntimeError lines: 0.
- `prepare_error_count=0`.
- WS snapshot had `active_stream_count=94`, `intended_stream_count=94`, `warm_symbols=45`, `fresh_tickers=598`, `fresh_mark_prices=716`, `fresh_book_tickers=45`, `fresh_klines_15m=45`.
- Emergency cycle completed with `shortlist_size=45`, `detector_runs=737`, `post_filter_candidates=2`, `raw_setups=737`, `rejected=735`.
- Per-symbol cycle telemetry logged 45 rows with `status=ok`.
- No `message buffer full` or Telegram flood-control lines appeared in the smoke logs.

Not confirmed:

- Real Telegram delivery was not exercised because the harness uses `FakeBroadcaster`.
- The smoke run produced `delivery_status_counts={'suppressed': 2}` and 0 delivered signals. This validates candidate selection and delivery orchestration up to the fake broadcaster boundary, not subscriber delivery.
- The run did not update `data/bot/telemetry/latest_run.json`; validation evidence is in the smoke stdout/stderr logs.

Artifacts:

- `logs/30_live_smoke_20260505_044859.out.log`
- `logs/30_live_smoke_20260505_044859.err.log`

## Final-Code Live Smoke

Command:

```powershell
python scripts\live_smoke_bot.py --warmup-seconds 5
```

Status: PASS for final-code smoke scope

Confirmed:

- Exit code: 0.
- Severity ERROR lines: 0.
- Severity WARNING lines: 0.
- Tracebacks: 0.
- RuntimeError lines: 0.
- `prepare_error_count=0`.
- Emergency cycle completed with `shortlist_size=45`, `detector_runs=774`, `post_filter_candidates=2`, `selected_signals=2`, `delivered_signals=0`, `delivery_status_counts={'suppressed': 2}`.
- WS snapshot had `active_stream_count=94`, `intended_stream_count=94`, `warm_symbols=45`, `fresh_tickers=591`, `fresh_mark_prices=716`, `fresh_book_tickers=45`, `fresh_klines_15m=45`.

Artifacts:

- `logs/31_live_smoke_final_20260505_052813.out.log`
- `logs/31_live_smoke_final_20260505_052813.err.log`

## RUN_2: 2-Hour Live Run

Status: NOT RUN

Reason: not completed in this session. No claim is made about 2-hour WS stability, SL rate after changes, or real Telegram statistics accuracy.

## RUN_3: Long-Run Stability

Status: NOT RUN

Reason: not completed in this session. No claim is made about 6-hour memory stability, long-run WS reconnect behavior, or telemetry completeness after the current patches.
