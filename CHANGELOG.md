# CHANGELOG - Audit 2026-05-26

## Critical Signal Safety

- Enforced delivery path: signal contract validation, then hard 3-of-5 confluence gate,
  then delivery.
- Added non-bypassable `ValueError` checks before appending and delivering signals.
- Added early return when all candidates are rejected before delivery.
- Proved invalid contracts stop before confluence/repository/delivery.
- Proved high-score weak signals stop at `stage=confluence`.
- Added float-safe TP1 R:R validation tolerance so exact 1.5R contracts are not rejected
  as `1.499999...`, while sub-1.5R signals remain blocked.

## Lookahead And Indicator Math

- Rewrote `_swing_points` in `bot/features.py` to avoid right-side lookahead and `shift(-N)`.
- Rewrote shared pivot/order-block detector paths in `bot/strategies/spec_patterns.py`.
- Replaced fallback RSI smoothing in shared spec patterns with project Wilder seed semantics.
- Made Bollinger fallback std explicit with `ddof=1`.
- Made `vwap_deviation_z20` std explicit with `ddof=1`.
- Removed the remaining legacy `shift(-N)` path from SMC swing helpers.
- Reworked `atr_expansion`, `aggression_shift`, and `stop_hunt_detection` to scan only
  bounded recent closed bars and reject stale drift by ATR.

## Shortlist And Config

- Added `PAXGUSDT` to required pinned symbols and config example.
- Added config assertion that PAXGUSDT is present.
- Verified dynamic shortlist scoring uses liquidity, spread/freshness, OI, funding/basis,
  crowding, and microstructure signals.
- Exported production strategy-fit routing to live/historical audit scripts so audits do
  not silently bypass shortlist strategy routing.
- Treated Binance `continuousKlines` `-4104 Invalid contract type` responses as optional
  unsupported derived history for metal-token symbols instead of noisy priority-history errors.
- Fixed live `public_intelligence` harmonic snapshot crash (`polars` import missing), caught by
  the first 20-minute smoke attempt.

## Strategy Auditability

- Strategy engine now emits explicit schedule-inactive skip results.
- Live strategy audit now reports 38 observed results per symbol, including session-gated skips.
- 57 generated signals passed signal contract checks in live surface audit.
- Added `scripts/historical_strategy_audit.py` for 30-day rolling closed-candle replay.
- Top-10 historical audit: 80 windows, 3040 detector runs, 38/38 strategies hit,
  0 signal-contract failures.

## Technical Debt

- Implemented monthly parquet compaction for old daily cache chunks.
- Removed production TODO/stub markers and renamed a false-positive placeholder constant.
- Added root navigation docs and hook copies for future agents.

## Verification

- `pytest -q`: 30 passed.
- `python scripts/live_check_binance_api.py`: REST and WS checks OK.
- `python scripts/live_check_indicators.py`: 3 successes, 0 failures.
- `python scripts/live_check_pipeline.py --symbols BTCUSDT ETHUSDT SOLUSDT --limit 3 --concurrency 2`: 3 dry-selected, 0 strategy errors.
- `python scripts/live_check_strategies.py --symbols BTCUSDT ETHUSDT SOLUSDT --limit 3 --concurrency 2 --require-signal-contract --summary-json data/bot/telemetry/strategy_audit_after_codex.json --print-summary-json`: 38 registered/evaluated, 57 contracts checked, 0 failures.
- `python scripts/historical_strategy_audit.py --symbols BTCUSDT ETHUSDT SOLUSDT BNBUSDT XRPUSDT DOGEUSDT ADAUSDT LINKUSDT AVAXUSDT LTCUSDT --days 30 --warmup-days 45 --window-step-bars 48 --max-windows-per-symbol 8 --concurrency 3 --require-registered 38 --require-contract-clean --require-no-zero-signals`: passed.
- `python scripts/live_smoke_bot.py --runtime-seconds 1200 ...`: live smoke artifact under `data/bot/telemetry/live_smoke_20260526_rerun/`.
