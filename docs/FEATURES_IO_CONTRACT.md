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

`_prepare_frame` returns a Polars DataFrame with core + advanced + oscillator + microstructure columns and drops warm-up rows where long-window features are not available (`ema200`, `donchian_low20`).

## Backward compatibility

Legacy wrappers in `bot.features` remain available and delegate to grouped modules (`bot.features_core`, `bot.features_advanced`, `bot.features_oscillators`).

## Regression parity

Parity is validated with decomposition regression tests:

- `tests/test_features_decomposition_parity.py`
- `tests/test_features_group_contracts.py`
