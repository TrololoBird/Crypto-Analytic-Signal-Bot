# 70 Final Validation

## Confirmed fixes

- Main branch was fast-forwarded to `codex/production-signal-hardening` and pushed before local runtime fixes.
- SQLite stale state was cleaned:
  - `SKYAIUSDT` / `wick_trap_reversal`
  - `1000LUNCUSDT` / `bos_choch`
  - `WLDUSDT` / `bos_choch`
- Startup now expires open signals older than 4 hours and purges cooldowns older than 2 hours.
- Binance USD-M websocket URLs now use routed endpoints:
  - `wss://fstream.binance.com/public`
  - `wss://fstream.binance.com/market`
- The missing runtime import contract `validate_runtime_public_rest_url()` was restored.
- `UniverseSymbol.strategy_fits` was restored, allowing live shortlists to enter strategy execution.
- Websocket buffering was increased and the message drain budget was raised.
- RR scoring now uses each setup's effective `min_rr` instead of assuming the global `1.9` floor.
- `min_score` was calibrated to `0.53` from live reject telemetry.
- Indicator telemetry now writes RSI, stochastic, CCI, Williams %R, OBV, and MFI snapshots to `features/indicator_snapshots.jsonl`.
- `XAUUSDT` and `XAGUSDT` were added to pinned symbols and the shortlist contract allowlist now includes `TRADIFI_PERPETUAL`.
- `bos_choch` now uses external SMC swing structure for stop placement.
- `wick_trap_reversal` now reads the wick-through ATR multiplier and base score from setup config.

## Candidate-producing validation

Run: `data/bot/telemetry/runs/20260502_101747_19800`

- Duration: about 10 minutes.
- Cycles: `418`.
- Detector runs: `3221`.
- Post-filter candidates: `4`.
- Selected/sent: `2`.
- Telegram delivery status: `{"sent": 2}`.
- Telegram message IDs from SQLite:
  - `PENDLEUSDT` / `bos_choch` / long: `4781`
  - `BSBUSDT` / `bos_choch` / long: `4782`

This run confirms the original zero-cycle / zero-detector / zero-candidate startup failure was fixed before the stricter SL changes.

## RUN_1: 15-minute live run

Run: `data/bot/telemetry/runs/20260502_105054_30364`

- Cycles: `712`.
- Detector runs: `5360`.
- Strategy raw signals: `306`.
- Post-filter candidates: `0`.
- Selected/sent: `0`.
- Errors: `0`.
- Tracebacks: `0`.
- Warnings: `0`.
- `XAUUSDT` cycles: `16`.
- `XAGUSDT` cycles: `16`.
- Indicator telemetry file: `data/bot/telemetry/runs/20260502_105054_30364/features/indicator_snapshots.jsonl`.

RUN_1 did not satisfy the requested `candidates > 0` criterion after the external-SL patch.

## RUN_2: 30-minute live run

Run: `data/bot/telemetry/runs/20260502_110946_22548`

- Cycles: `1433`.
- Detector runs: `10881`.
- Strategy raw signals: `568`.
- Post-filter candidates: `0`.
- Selected/sent: `0`.
- Errors: `0`.
- Tracebacks: `0`.
- Warnings: `0`.
- `XAUUSDT` cycles: `34`.
- `XAGUSDT` cycles: `34`.
- Latest shortlist pinned count: `6`.
- Latest shortlist top symbols included `ETHUSDT`, `BTCUSDT`, `SOLUSDT`, `XRPUSDT`, `XAUUSDT`, `XAGUSDT`.
- Indicator telemetry file: `data/bot/telemetry/runs/20260502_110946_22548/features/indicator_snapshots.jsonl`.
- Open signals at end: `PENDLEUSDT` and `BSBUSDT`, both from the candidate-producing run, both active, no stop-loss close recorded during this validation window.

RUN_2 did not satisfy the requested `selected > 0` criterion in the final stricter build.

## Residual risk

- The runtime pipeline is fixed: cycles, detector runs, websocket data, shortlist formation, metals data, and indicator telemetry are all confirmed live.
- The final build is stricter than the candidate-producing build because BOS/CHoCH stops now require external SMC swings. In the 30-minute final run, `data.external_swing_stop_missing_short` occurred `113` times and `data.external_swing_stop_missing_long` occurred `5` times.
- Because the final run produced no new selected signals, Telegram message IDs for final-run signals do not exist. The confirmed Telegram IDs are from the candidate-producing run: `4781` and `4782`.
