# Strategies

## Overview

Strategies are implemented in `bot/strategies/` and registered in the modern registry.  
Each strategy produces a `StrategyDecision` that can be accepted, rejected, skipped, or errored.

## Pipeline

1. Frame preparation (`bot/features.py`)
2. Strategy detection (`bot/core/engine.py`)
3. Family precheck + confirmation (`bot/application/symbol_analyzer.py`)
4. Global filters (`bot/filters.py`)
5. Confluence scoring (`bot/confluence.py`)
6. Delivery + tracking (`bot/application/delivery_orchestrator.py`)

## Family model

- `continuation`
- `breakout`
- `reversal`
- `orderbook`
- `orderflow`
- `liquidity`
- `volatility`
- `sentiment`
- `multi_asset`

Family and confirmation profile metadata are attached per strategy and used by symbol-level context checks.
- Continuation/breakout families treat crowd positioning as confirmation/headwind context.
- Reversal families treat crowd positioning as exhaustion context and should not be rejected by continuation-style crowd rules.
- Roadmap families currently use the same confirmation profiles (`trend_follow`,
  `breakout_acceptance`, `countertrend_exhaustion`) and public-data fields on
  `PreparedSymbol`; they are signal detectors, not execution modules.

## Operational notes

- Keep strategy params in `[bot.filters.setups]` config scope.
- Keep setup enable flags in `[bot.setups]`.
- All registered setup flags must remain enabled unless a strategy is removed
  from `STRATEGY_CLASSES`; broken strategies are fixed through detector logic,
  explicit reject reasons, or config calibration rather than hidden disablement.
- `bot.filters.min_risk_reward` is the default RR floor. Per-setup `min_rr`
  values explicitly override it for that detector.
- A signal that opposes confirmed 1h trend context is rejected by the global
  `trend_conflict_1h` filter.
- Telegram `why now` companion messages are optional and controlled by
  `bot.notifiers.send_analytics_companion`; the main signal card remains the
  canonical delivery format.
- Add regression coverage when changing decision contracts or metadata.
- When changing structural target logic, preserve the short-side rule that stop anchors come from resistance above entry, not from the nearest arbitrary structure.
- Runtime params must affect detection or target construction, not just defaults:
  - `funding_reversal`: `funding_trend_bars`, `min_delta_threshold`, `sl_buffer_atr`
  - `cvd_divergence`: `min_delta_threshold`, `sl_buffer_atr`
  - `hidden_divergence`: `rsi_divergence_lookback`, `rsi_divergence_threshold`, `min_delta_threshold`, `sl_buffer_atr`
  - `squeeze_setup`: `bb_squeeze_threshold`, `min_bb_compression_width`, `bb_pct_b_threshold`, `volume_threshold`, `sl_buffer_atr`
  - `wick_trap_reversal`: `wick_through_atr_mult`, `closed_back_threshold`, `sl_buffer_atr`

## 2026-05-03 Roadmap Expansion

Added detectors:

- Orderbook: `whale_walls`, `spread_strategy`, `depth_imbalance`
- Order Flow: `absorption`, `aggression_shift`
- Liquidity: `liquidation_heatmap`, `stop_hunt_detection`
- Trend Following: `multi_tf_trend` plus existing `vwap_trend`, `supertrend_follow`
- Pump & Dump: existing `volume_anomaly`, `price_velocity`
- Bottom/Top Picking: `rsi_divergence_bottom`, `wyckoff_spring`, existing `volume_climax_reversal`
- Volatility: `bb_squeeze`, `atr_expansion`, existing `squeeze_setup`
- Sentiment: `ls_ratio_extreme`, `oi_divergence`
- Multi-Asset: `btc_correlation`, `altcoin_season_index`

Implementation caveat: several detectors are conservative public-data proxies
for concepts that would need richer historical orderbook or liquidation-cluster
data for full parity. Examples: `whale_walls` uses depth/microprice imbalance
from public WS partial-depth when fresh and public REST `/fapi/v1/depth` as the
cold/stale fallback, and `liquidation_heatmap` uses recent liquidation sentiment
rather than a true exchange-wide liquidation heatmap.

## 2026-05-14 Live Audit

See `docs/strategy_live_audit_2026-05-14.md` for the live audit against the
running process, telemetry, outcome database, Telegram delivery format, Binance
public-data docs, and the current 37-strategy matrix.
