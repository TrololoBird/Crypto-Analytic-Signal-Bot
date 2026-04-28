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

Family and confirmation profile metadata are attached per strategy and used by symbol-level context checks.
- Continuation/breakout families treat crowd positioning as confirmation/headwind context.
- Reversal families treat crowd positioning as exhaustion context and should not be rejected by continuation-style crowd rules.

## Operational notes

- Keep strategy params in `[bot.filters.setups]` config scope.
- Keep setup enable flags in `[bot.setups]`.
- Add regression coverage when changing decision contracts or metadata.
- When changing structural target logic, preserve the short-side rule that stop anchors come from resistance above entry, not from the nearest arbitrary structure.
- Runtime params must affect detection or target construction, not just defaults:
  - `funding_reversal`: `funding_trend_bars`, `min_delta_threshold`, `sl_buffer_atr`
  - `cvd_divergence`: `min_delta_threshold`, `sl_buffer_atr`
  - `hidden_divergence`: `rsi_divergence_lookback`, `rsi_divergence_threshold`, `min_delta_threshold`, `sl_buffer_atr`
  - `squeeze_setup`: `bb_squeeze_threshold`, `min_bb_compression_width`, `bb_pct_b_threshold`, `volume_threshold`, `sl_buffer_atr`
