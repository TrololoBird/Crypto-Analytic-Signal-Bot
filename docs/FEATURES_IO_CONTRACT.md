# Features I/O Contract

## Thematic API groups

`bot.features` exports three explicit API groups:

- `CORE_API` — trend/volatility/base context (`ema`, `rsi`, `atr`, `adx`, `vwap`, `roc`, `realized_volatility`, `safe_close_position`, `add_core_features`).
- `ADVANCED_API` — advanced context (`supertrend`, `add_advanced_indicators`).
- `OSCILLATORS_API` — oscillator signals (`stochastic`, `cci`, `mfi`, `cmf`, `ultimate_oscillator`, `add_oscillator_features`).

## Input contract

For full pipeline (`_prepare_frame`) input frame must include:

- OHLCV: `open`, `high`, `low`, `close`, `volume`
- Time columns: `open_time`, `close_time`
- Optional microstructure fields for enriched outputs: `taker_buy_base_volume`, `quote_volume`, `trades`, `taker_buy_quote_volume`

## Output contract

`_prepare_frame` returns a Polars DataFrame with core + advanced + oscillator + microstructure + session columns and drops warm-up rows where long-window features are not available (`ema200`, `donchian_low20`).

`prepare_symbol()` preserves timeframe names literally in `PreparedSymbol`.
Current runtime code requires all configured warmup minimums for `5m`, `15m`,
`1h`, and `4h` before it returns a `PreparedSymbol`; missing or short history
returns `None` with an `insufficient frame data` log line. The field names then
retain their literal timeframe meaning:

- `work_15m` is always the prepared 15m frame.
- `work_1h` is always the prepared 1h frame.
- `work_5m` is the prepared 5m frame once the symbol passes warmup.
- `work_4h` is the prepared 4h frame once the symbol passes warmup.
- `work_primary` points to the configured primary timeframe frame (`5m`, `15m`, `1h`, or `4h`) after fallback resolution.

Asset-level `primary_timeframe` changes freshness/scoring policy and the
explicit `work_primary` pointer. It must not alias a 1h or 4h frame into
`work_15m`, because strategies, telemetry, outcomes, and diagnostics treat the
field name as part of the runtime contract.

Microstructure fields must carry provenance when they are promoted to
`PreparedSymbol`:

- `funding_rate` is the current public USD-M funding snapshot. When funding
  history has been warmed from `/fapi/v1/fundingRate`,
  `funding_recent_extreme_rate` and `funding_recent_extreme_age_hours` expose
  the strongest real funding print inside the runtime lookback so
  `funding_reversal` does not treat a normalized current snapshot as proof that
  no recent funding dislocation existed.
- `depth_imbalance_source` and `microprice_bias_source` distinguish `l2_depth`,
  `l1_book`, `rest_book_l1`, and `agg_trade_proxy`.
- `depth_book_age_seconds` is populated when a partial depth book is available.
- `orderflow_source` identifies live `agg_trade` flow versus weaker REST/candle
  proxies.
- `liquidation_score_source="force_order"` is required before
  `liquidation_heatmap` can treat `liquidation_score` as real liquidation
  context. OHLCV wick/volume exhaustion is a separate proxy and must not be
  emitted as a force-order heatmap signal.

Session fields currently emitted by the runtime feature path:

- `session_asia`, `session_london`, `session_ny`, `session_overlap`
- `session_asia_vol_20`, `session_london_vol_20`, `session_ny_vol_20`, `session_overlap_vol_20`

The runtime indicator path is Polars-native and now prefers installed
`polars_ta` expressions for compatible core indicators. Project-specific
normalization still applies after materialization; for example, `polars_ta.RSI`
is normalized from `0..1` to the bot contract of `0..100`. Pure-Polars formulas
remain deterministic fallbacks when the installed backend is unavailable or a
specific expression fails.

## Backward compatibility

Legacy wrappers in `bot.features` remain available and delegate to grouped modules (`bot.features_core`, `bot.features_advanced`, `bot.features_oscillators`).

Shared dataframe/helper primitives are centralized in `bot.features_shared` (for example: `materialize_series`, `clean_non_finite`, `true_range`, `atr_from_true_range`, and input validators), and group modules consume those helpers directly.

## Verification

The generated regression tests were removed. Feature contract changes should be
validated with compile/import checks, config validation, and read-only live
diagnostics such as:

- `python -m compileall bot`
- `python -m scripts.validate_config`
- `python -m scripts.live_check_indicators --symbols BTCUSDT ETHUSDT`
- `python -m scripts.live_check_pipeline --limit 4 --concurrency 1 --no-warm-context`
