# Phase 1 Report

Generated: 2026-05-06

## Confirmed Facts

- Runtime entry remains `main.py` -> `bot.cli.run()` -> `bot.application.bot.SignalBot`.
- Registered strategy count is 37, verified by `python -c "from bot.strategies import STRATEGY_CLASSES; print(len(STRATEGY_CLASSES))"`.
- `config.toml` and `config.toml.example` both declare pinned priority assets: `BTCUSDT`, `ETHUSDT`, `XAUUSDT`, `XAGUSDT`.
- `config.toml` and `config.toml.example` both declare per-asset timeframe overrides. Metals use `primary_timeframe = "1h"` and `context_timeframes = ["4h"]`.
- Current REST path scan found USD-M public market endpoints under `/fapi/v1/*` and `/futures/data/*`; endpoint-policy tests cover private/account/order rejection.
- Official Binance docs checked on 2026-05-06 confirm `GET /fapi/v1/exchangeInfo`, `GET /fapi/v1/fundingRate`, `GET /fapi/v1/openInterest`, `/futures/data/openInterestHist`, and routed WS endpoints `/public` and `/market`.

## Telemetry Inputs

- Latest telemetry run analyzed: `data/bot/telemetry/runs/20260504_045105_36740`.
- Latest run size: approximately 515 MB across 65 files.
- Analyzer report: `logs/telemetry_calibration_analysis.md`.
- 72-hour tail-limited analyzer report: `logs/telemetry_calibration_analysis_72h.md`.
- Analyzer now reads the tail of large JSONL files instead of materializing full telemetry files.

## Calibration Snapshot

- Tail-limited 72-hour analysis loaded 27,724 records.
- Priority asset signal counts in that tail-limited sample:
  - `BTCUSDT`: 620 rows, 0 signals.
  - `ETHUSDT`: 603 rows, 0 signals.
  - `XAUUSDT`: 551 rows, 0 signals.
  - `XAGUSDT`: 569 rows, 0 signals.
- Top reject reasons in the 72-hour report include `pattern.volume_too_low`, `data.orderbook_context_missing`, `data.depth_imbalance_missing`, `data.btc_context_missing`, and strategy-specific pattern misses.

## Latest Local Validation Artifacts

- `logs/30_validation.md` records a 30-minute live smoke and a final short live smoke.
- Final smoke summary confirms `selected_signals=2`, `prepare_error_count=0`, public and market WS endpoints connected, and `reconnect_count=2`.
- `LIVE_VALIDATION.md` records a stricter validation failure for the memory-sampled run because three handled strategy timeouts were logged.

## Missing Or Incomplete Evidence

- No current run in the last 24 hours contains enough strategy telemetry to prove fresh 24-hour production behavior.
- Memory flatness is not proven by runtime telemetry; current evidence is external Windows WorkingSet sampling only.
- Real Telegram delivery was not verified; smoke harness used disabled/fake delivery boundaries.
