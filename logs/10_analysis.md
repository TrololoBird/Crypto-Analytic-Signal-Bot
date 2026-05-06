# Phase 1 Deep Analysis

Generated: 2026-05-05T02:35:31+00:00

## Evidence Boundary

- Confirmed: this report is derived from refreshed Phase 0 manifests, logs, telemetry, and the read-only SQLite database.
- Confirmed: code-path claims were checked against current source after Phase 0.
- Uncertainty: telemetry covers historical runs in this workspace; it cannot prove post-change live behavior until a new live run is analyzed.

## Confirmed High-Level State

- Files scanned: 2408
- Telemetry files scanned: 1586
- Parsed telemetry records: 1,157,161
- Error-like lines: 71
- Warning-like lines: 84
- Inferred SL-like rate: 58.54% (48/82)
- Inferred TP/win-like rate: 41.46% (34/82)
- Dashboard strategy registration check: current code exposes 37 strategy classes and 37 enabled config IDs.

## Current Database State

| Status | Count |
| --- | --- |
| closed | 98 |

- Tracking counters: signals_sent=98, activated=92, tp1_hit=20, tp2_hit=14, stop_loss=51, expired=38.

## Strategy Telemetry

| Strategy | Decision Rows | Signal Decisions | Signal Rate | Selected Rows | Top Reject |
| --- | --- | --- | --- | --- | --- |
| fvg_setup | 19,539 | 3,866 | 19.79% | 0 | pattern.no_fvg_detected |
| liquidity_sweep | 18,461 | 0 | 0.00% | 0 | pattern.no_liquidity_sweep_detected |
| cvd_divergence | 15,399 | 1,461 | 9.49% | 7 | pattern.no_cvd_divergence_detected |
| structure_pullback | 15,399 | 1,493 | 9.70% | 12 | pattern.volume_too_low |
| ema_bounce | 15,397 | 911 | 5.92% | 1 | pattern.no_bounce_pattern |
| vwap_trend | 11,844 | 102 | 0.86% | 0 | pattern.volume_too_low |
| turtle_soup | 11,224 | 174 | 1.55% | 3 | pattern.no_false_breakout_detected |
| hidden_divergence | 11,224 | 20 | 0.18% | 1 | pattern.volume_too_low |
| wick_trap_reversal | 11,220 | 475 | 4.23% | 5 | pattern.no_wick_trap_detected |
| structure_break_retest | 10,283 | 0 | 0.00% | 0 | pattern.no_breakout_detected |
| session_killzone | 10,283 | 300 | 2.92% | 0 | context.outside_killzone |
| breaker_block | 10,282 | 159 | 1.55% | 0 | pattern.no_breaker_block_detected |
| bos_choch | 10,279 | 5,684 | 55.30% | 16 | pattern.insufficient_swing_points |
| order_block | 10,278 | 68 | 0.66% | 0 | pattern.no_order_block_detected |
| squeeze_setup | 10,276 | 0 | 0.00% | 0 | indicator.no_bb_kc_squeeze |
| keltner_breakout | 9,584 | 572 | 5.97% | 1 | pattern.volume_too_low |
| spread_strategy | 9,580 | 1,445 | 15.08% | 14 | pattern.volume_too_low |
| depth_imbalance | 9,579 | 0 | 0.00% | 0 | data.depth_imbalance_missing |
| whale_walls | 9,579 | 0 | 0.00% | 0 | data.orderbook_context_missing |
| funding_reversal | 8,906 | 0 | 0.00% | 0 | indicator.funding_not_extreme |
| supertrend_follow | 8,417 | 375 | 4.46% | 1 | pattern.volume_too_low |
| multi_tf_trend | 8,413 | 602 | 7.16% | 1 | pattern.volume_too_low |
| btc_correlation | 8,412 | 282 | 3.35% | 0 | data.btc_context_missing |
| altcoin_season_index | 8,412 | 0 | 0.00% | 0 | data.altcoin_season_index_missing |
| volume_climax_reversal | 5,433 | 0 | 0.00% | 0 | data.volume_climax_missing |
| liquidation_heatmap | 5,429 | 1,527 | 28.13% | 18 | data.liquidation_score_missing |
| stop_hunt_detection | 5,429 | 0 | 0.00% | 0 | pattern.volume_too_low |
| rsi_divergence_bottom | 5,429 | 374 | 6.89% | 1 | data.rsi_divergence_missing |
| wyckoff_spring | 5,429 | 0 | 0.00% | 0 | pattern.volume_too_low |
| absorption | 5,429 | 98 | 1.81% | 0 | pattern.absorption_not_confirmed |
| price_velocity | 4,561 | 282 | 6.18% | 5 | pattern.volume_too_low |
| volume_anomaly | 4,561 | 109 | 2.39% | 2 | data.volume_spike_missing |
| aggression_shift | 4,558 | 2 | 0.04% | 0 | pattern.volume_too_low |
| bb_squeeze | 4,557 | 69 | 1.51% | 1 | indicator.bb_squeeze_not_active |
| atr_expansion | 4,557 | 146 | 3.20% | 3 | indicator.atr_expansion_too_low |
| oi_divergence | 3,497 | 0 | 0.00% | 0 | indicator.oi_price_divergence_too_small |
| ls_ratio_extreme | 3,497 | 541 | 15.47% | 6 | pattern.ls_ratio_not_extreme |

### Zero-Hit Strategies

- altcoin_season_index
- depth_imbalance
- funding_reversal
- liquidity_sweep
- oi_divergence
- squeeze_setup
- stop_hunt_detection
- structure_break_retest
- volume_climax_reversal
- whale_walls
- wyckoff_spring

### Top Rejections

| Reason | Count |
| --- | --- |
| pattern.volume_too_low | 116,512 |
| pattern.no_liquidity_sweep_detected | 36,889 |
| pattern.no_bounce_pattern | 28,401 |
| pattern.no_fvg_detected | 21,942 |
| pattern.no_cvd_divergence_detected | 20,951 |
| pattern.no_breakout_detected | 20,529 |
| indicator.no_bb_kc_squeeze | 20,528 |
| pattern.no_false_breakout_detected | 20,522 |
| pattern.no_breaker_block_detected | 20,173 |
| data.orderbook_context_missing | 19,154 |
| data.depth_imbalance_missing | 19,152 |
| pattern.no_order_block_detected | 18,365 |
| data.altcoin_season_index_missing | 16,181 |
| pattern.no_wick_trap_detected | 16,086 |
| indicator.funding_not_extreme | 15,551 |
| context.outside_killzone | 13,633 |
| indicator.adx_too_low | 12,354 |
| data.btc_context_missing | 11,097 |
| pattern.absorption_not_confirmed | 10,658 |
| data.volume_climax_missing | 10,408 |

## Outcome-Derived Calibration Targets

| Setup | Total | Stop Loss | Profitable | Avg R |
| --- | --- | --- | --- | --- |
| liquidation_heatmap | 15 | 11 | 6 | -0.040 |
| structure_pullback | 11 | 7 | 3 | -0.388 |
| spread_strategy | 10 | 9 | 0 | -0.942 |

## Area Analysis

### Data Acquisition

- Confirmed: REST and WS are both active paths. REST fetches USD-M public endpoints; WS uses public/market stream routes. Endpoint-policy tests reject private/auth routes.
- Confirmed problem: historical logs contain websocket buffer backpressure and REST timeout warnings. Current config already limits WS kline intervals to 15m and uses REST cache for context frames.
- Inference: the highest remaining acquisition risk is missing or stale derivative context (`ls_ratio`, `oi_change_pct`) during startup or before OI refresh completes.

### Indicator Engine

- Confirmed: features are prepared through Polars frames and `prepare_symbol`; missing required 15m/1h history blocks analysis.
- Confirmed problem: data-quality telemetry repeatedly reports missing `ls_ratio` and `oi_change_pct`; strategies depending on these fields should fail explicitly rather than silently degrade.

### Strategy Logic

- Confirmed: 37 strategies are registered and enabled in current code/config.
- Confirmed problem: historical telemetry shows 11 zero-hit strategies and a top rejection reason of `pattern.volume_too_low`.
- Confirmed problem: `spread_strategy`, `liquidation_heatmap`, and `structure_pullback` have >=10 stored outcomes and >50% stop-loss share.

### Signal Lifecycle

- Confirmed: active lifecycle is stored in `active_signals`/`signal_outcomes`; legacy `signals`/`outcomes` tables are empty in current DB.
- Confirmed problem fixed in current patch: mild negative setup-score adjustments were acting as hard suppressors before global filters could apply calibrated scoring.

### Shortlist And Universe

- Confirmed: shortlist has `rest_full`, `ws_light`, `cached`, and `pinned_fallback` paths with telemetry for fallback reason.
- Historical issue: prior logs had `strategy_fits` constructor/attribute errors. Current model and shortlist code expose `strategy_fits`; the specific old error is not reproducible from current source.

### Risk Management

- Confirmed: SL rate from persisted tracking events is above the requested 30% target. Evidence supports tightening poor setup entry filters and widening stops for high-SL setups.
- Uncertainty: sample sizes remain small for many setups; broad parameter sweeps would need a backtest/forward-test tool before larger changes are justified.

### Telegram Delivery

- Confirmed problem: logs contain Telegram flood-control failures. Current patch adds local pacing before Telegram sends/edits/photos instead of waiting for server-side rate-limit errors.

### Dashboard

- Confirmed: current dashboard strategy cache returns 37 strategies, so the historical 33/37 defect appears fixed in this worktree.
- Residual risk: endpoint liveness still requires a running dashboard server check after live startup.

### ML And Analytics

- Confirmed: ML live use is disabled in config. Analytics uses persisted tracked outcomes for dashboard-compatible strategy reports.
- Inference: ML modules should remain non-gating until outcome volume is larger and labels are cleaner.

### Performance

- Confirmed: historical WS buffer compaction and Telegram rate limits are operational bottlenecks. Current config and patch reduce bursts but do not prove long-run stability.
