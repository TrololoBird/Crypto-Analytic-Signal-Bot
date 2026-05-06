# Calibration Log

## 2026-05-06

### Confirmed

- Registered strategy count: 37.
- Priority assets are pinned in config: `BTCUSDT`, `ETHUSDT`, `XAUUSDT`, `XAGUSDT`.
- Metal priority assets use `primary_timeframe = "1h"` and `context_timeframes = ["4h"]`.
- Asset-fit tests cover BTC exclusion for `btc_correlation`, ETH exclusion for `altcoin_season_index`, orderbook low-liquidity rejection, and config-driven exclusion.
- BOS/CHoCH stop fallback coverage now exercises the internal-swing fallback path with ATR/previous-candle fallback intentionally disabled.
- Telemetry analyzer now reads large JSONL files using bounded tail reads and recognizes both `strategy_id` and `setup_id`.

### Calibration Data

- Latest analyzed run: `20260504_045105_36740`.
- Tail-limited 72-hour report: `logs/telemetry_calibration_analysis_72h.md`.
- Priority assets in that sample still show 0 emitted signals.
- Latest strict validation record: `LIVE_VALIDATION.md`; status is not a clean pass because the memory-sampled run logged three handled strategy timeouts.

### Unverified

- Fresh post-change 60-minute priority-asset live validation.
- Real Telegram channel delivery.
- Long-run memory stability from runtime telemetry.
