# CHANGELOG - Audit 2026-05-26

## Critical Signal Safety

- Enforced delivery path: signal contract validation, then hard 3-of-5 confluence gate,
  then delivery.
- Added non-bypassable `ValueError` checks before appending and delivering signals.
- Added early return when all candidates are rejected before delivery.
- Proved invalid contracts stop before confluence/repository/delivery.
- Proved high-score weak signals stop at `stage=confluence`.

## Lookahead And Indicator Math

- Rewrote `_swing_points` in `bot/features.py` to avoid right-side lookahead and `shift(-N)`.
- Rewrote shared pivot/order-block detector paths in `bot/strategies/spec_patterns.py`.
- Replaced fallback RSI smoothing in shared spec patterns with project Wilder seed semantics.
- Made Bollinger fallback std explicit with `ddof=1`.
- Made `vwap_deviation_z20` std explicit with `ddof=1`.

## Shortlist And Config

- Added `PAXGUSDT` to required pinned symbols and config example.
- Added config assertion that PAXGUSDT is present.
- Verified dynamic shortlist scoring uses liquidity, spread/freshness, OI, funding/basis,
  crowding, and microstructure signals.

## Strategy Auditability

- Strategy engine now emits explicit schedule-inactive skip results.
- Live strategy audit now reports 38 observed results per symbol, including session-gated skips.
- 57 generated signals passed signal contract checks in live surface audit.

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
