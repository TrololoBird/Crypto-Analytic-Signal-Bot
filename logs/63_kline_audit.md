# 63 Kline Pipeline Audit

## Confirmed facts

- Live run inspected: `data/bot/logs/bot_20260502_093802_20260.log`.
- Runtime telemetry directory: `data/bot/telemetry/runs/20260502_093802_20260`.
- The bot completed `211` cycles in the 5-minute audit window.
- Detector runs were non-zero: `1596`.
- Raw strategy hits were non-zero: `98`.
- The shortlist stayed populated at `45` symbols.
- Runtime health reported:
  - `active_stream_count=94`
  - `intended_stream_count=94`
  - `warm_symbols=45`
  - `fresh_book_tickers=45`
  - `fresh_klines_15m=45`
  - `fresh_tickers=592`
  - `fresh_mark_prices=716`
- No `message buffer full` warning was found in this 5-minute run after increasing the WS buffer/drain budget.

## Code path verified

- `bot/application/symbol_analyzer.py` fetches frames through `fetch_frames()`.
- `bot/ws_manager.py` supplies warm websocket 15m frames through `get_symbol_frames()`.
- `bot/application/symbol_analyzer.py` falls back to cached REST klines for missing `5m`, `1h`, and `4h` frames.
- `bot/features.py::prepare_symbol()` builds the prepared symbol and sets the frame-dependent features consumed by strategies and filters.

## Findings

- The original reported state, `required_frame_readiness={5m: 0, 15m: 0, 1h: 0, 4h: 0}`, was not reproduced after the WS endpoint and config fixes.
- Kline ingestion is now confirmed alive for `15m` websocket data and REST-backed higher/lower timeframe history.
- Candidate output stayed at `0` in this audit run, but that was not caused by missing klines. Rejection pressure was downstream:
  - strategy rejects: `1438`
  - filter rejects: `73`
  - confirmation rejects: `45`
  - data-quality rejects: `25`
  - family precheck rejects: `15`

## Fixes attached to this audit

- Restored routed Binance USD-M websocket endpoints in `bot/config.py`.
- Increased websocket buffer and drain capacity in `bot/ws_manager.py`.
- Restored missing import contract `validate_runtime_public_rest_url()` in `bot/market_data.py`.
- Added missing `UniverseSymbol.strategy_fits` in `bot/models.py`, which allowed shortlist rows to enter strategy execution.
