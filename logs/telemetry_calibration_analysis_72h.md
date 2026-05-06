# Telemetry Calibration Analysis

Run: `20260504_045105_36740`
Records: 27724
Generated: 2026-05-06T01:24:57+00:00

## Strategy Performance

| Strategy | Rows | Signals | Signal/hour | Top Reject |
| --- | ---: | ---: | ---: | --- |
| `absorption` | 585 | 0 | 0.0 | `pattern.absorption_not_confirmed` |
| `aggression_shift` | 479 | 0 | 0.0 | `pattern.volume_too_low` |
| `altcoin_season_index` | 1160 | 0 | 0.0 | `data.altcoin_season_index_missing` |
| `atr_expansion` | 493 | 0 | 0.0 | `indicator.atr_expansion_too_low` |
| `bb_squeeze` | 228 | 0 | 0.0 | `indicator.bb_squeeze_not_active` |
| `bos_choch` | 481 | 0 | 0.0 | `pattern.raw_hit` |
| `breaker_block` | 476 | 0 | 0.0 | `pattern.no_breaker_block_detected` |
| `btc_correlation` | 904 | 0 | 0.0 | `data.btc_context_missing` |
| `cvd_divergence` | 1035 | 0 | 0.0 | `pattern.no_cvd_divergence_detected` |
| `depth_imbalance` | 1040 | 0 | 0.0 | `data.depth_imbalance_missing` |
| `ema_bounce` | 1007 | 0 | 0.0 | `pattern.no_bounce_pattern` |
| `funding_reversal` | 495 | 0 | 0.0 | `indicator.funding_not_extreme` |
| `fvg_setup` | 1316 | 0 | 0.0 | `pattern.no_fvg_detected` |
| `hidden_divergence` | 453 | 0 | 0.0 | `pattern.volume_too_low` |
| `keltner_breakout` | 1061 | 0 | 0.0 | `pattern.volume_too_low` |
| `liquidation_heatmap` | 804 | 0 | 0.0 | `unknown` |
| `liquidity_sweep` | 1103 | 0 | 0.0 | `pattern.no_liquidity_sweep_detected` |
| `ls_ratio_extreme` | 568 | 0 | 0.0 | `pattern.ls_ratio_not_extreme` |
| `multi_tf_trend` | 1106 | 0 | 0.0 | `pattern.volume_too_low` |
| `oi_divergence` | 399 | 0 | 0.0 | `indicator.oi_price_divergence_too_small` |
| `order_block` | 478 | 0 | 0.0 | `pattern.no_order_block_detected` |
| `price_velocity` | 531 | 0 | 0.0 | `pattern.volume_too_low` |
| `rsi_divergence_bottom` | 348 | 0 | 0.0 | `data.rsi_divergence_missing` |
| `session_killzone` | 168 | 0 | 0.0 | `pattern.average_volume_too_low` |
| `spread_strategy` | 1188 | 0 | 0.0 | `pattern.volume_too_low` |
| `squeeze_setup` | 476 | 0 | 0.0 | `indicator.no_bb_kc_squeeze` |
| `stop_hunt_detection` | 333 | 0 | 0.0 | `pattern.volume_too_low` |
| `structure_break_retest` | 483 | 0 | 0.0 | `pattern.no_breakout_detected` |
| `structure_pullback` | 1149 | 0 | 0.0 | `filter.trend_score_too_low` |
| `supertrend_follow` | 1084 | 0 | 0.0 | `pattern.volume_too_low` |
| `turtle_soup` | 222 | 0 | 0.0 | `pattern.no_false_breakout_detected` |
| `volume_anomaly` | 248 | 0 | 0.0 | `data.volume_spike_missing` |
| `volume_climax_reversal` | 589 | 0 | 0.0 | `data.volume_climax_missing` |
| `vwap_trend` | 1538 | 0 | 0.0 | `pattern.volume_too_low` |
| `whale_walls` | 613 | 0 | 0.0 | `data.orderbook_context_missing` |
| `wick_trap_reversal` | 602 | 0 | 0.0 | `pattern.no_wick_trap_detected` |
| `wyckoff_spring` | 586 | 0 | 0.0 | `pattern.volume_too_low` |

## Calibration Issues

- `medium` `zero_signal_strategy` `absorption`: 585 decisions, 0 signals
- `medium` `high_reject_rate` `absorption`: 585/585 rejects
- `medium` `zero_signal_strategy` `aggression_shift`: 479 decisions, 0 signals
- `medium` `high_reject_rate` `aggression_shift`: 479/479 rejects
- `medium` `zero_signal_strategy` `altcoin_season_index`: 1160 decisions, 0 signals
- `medium` `high_reject_rate` `altcoin_season_index`: 1160/1160 rejects
- `medium` `zero_signal_strategy` `atr_expansion`: 493 decisions, 0 signals
- `medium` `high_reject_rate` `atr_expansion`: 493/493 rejects
- `medium` `zero_signal_strategy` `bb_squeeze`: 228 decisions, 0 signals
- `medium` `high_reject_rate` `bb_squeeze`: 228/228 rejects
- `medium` `zero_signal_strategy` `bos_choch`: 481 decisions, 0 signals
- `medium` `high_reject_rate` `bos_choch`: 481/481 rejects
- `medium` `zero_signal_strategy` `breaker_block`: 476 decisions, 0 signals
- `medium` `high_reject_rate` `breaker_block`: 476/476 rejects
- `medium` `zero_signal_strategy` `btc_correlation`: 904 decisions, 0 signals
- `medium` `high_reject_rate` `btc_correlation`: 904/904 rejects
- `medium` `zero_signal_strategy` `cvd_divergence`: 1035 decisions, 0 signals
- `medium` `high_reject_rate` `cvd_divergence`: 1035/1035 rejects
- `medium` `zero_signal_strategy` `depth_imbalance`: 1040 decisions, 0 signals
- `medium` `high_reject_rate` `depth_imbalance`: 1040/1040 rejects
- `medium` `zero_signal_strategy` `ema_bounce`: 1007 decisions, 0 signals
- `medium` `high_reject_rate` `ema_bounce`: 1007/1007 rejects
- `medium` `zero_signal_strategy` `funding_reversal`: 495 decisions, 0 signals
- `medium` `high_reject_rate` `funding_reversal`: 495/495 rejects
- `medium` `zero_signal_strategy` `fvg_setup`: 1316 decisions, 0 signals
- `medium` `high_reject_rate` `fvg_setup`: 1316/1316 rejects
- `medium` `zero_signal_strategy` `hidden_divergence`: 453 decisions, 0 signals
- `medium` `high_reject_rate` `hidden_divergence`: 453/453 rejects
- `medium` `zero_signal_strategy` `keltner_breakout`: 1061 decisions, 0 signals
- `medium` `high_reject_rate` `keltner_breakout`: 1061/1061 rejects
- `medium` `zero_signal_strategy` `liquidation_heatmap`: 804 decisions, 0 signals
- `medium` `high_reject_rate` `liquidation_heatmap`: 804/804 rejects
- `medium` `zero_signal_strategy` `liquidity_sweep`: 1103 decisions, 0 signals
- `medium` `high_reject_rate` `liquidity_sweep`: 1103/1103 rejects
- `medium` `zero_signal_strategy` `ls_ratio_extreme`: 568 decisions, 0 signals
- `medium` `high_reject_rate` `ls_ratio_extreme`: 568/568 rejects
- `medium` `zero_signal_strategy` `multi_tf_trend`: 1106 decisions, 0 signals
- `medium` `high_reject_rate` `multi_tf_trend`: 1106/1106 rejects
- `medium` `zero_signal_strategy` `oi_divergence`: 399 decisions, 0 signals
- `medium` `high_reject_rate` `oi_divergence`: 399/399 rejects
- `medium` `zero_signal_strategy` `order_block`: 478 decisions, 0 signals
- `medium` `high_reject_rate` `order_block`: 478/478 rejects
- `medium` `zero_signal_strategy` `price_velocity`: 531 decisions, 0 signals
- `medium` `high_reject_rate` `price_velocity`: 531/531 rejects
- `medium` `zero_signal_strategy` `rsi_divergence_bottom`: 348 decisions, 0 signals
- `medium` `high_reject_rate` `rsi_divergence_bottom`: 348/348 rejects
- `medium` `zero_signal_strategy` `session_killzone`: 168 decisions, 0 signals
- `medium` `high_reject_rate` `session_killzone`: 168/168 rejects
- `medium` `zero_signal_strategy` `spread_strategy`: 1188 decisions, 0 signals
- `medium` `high_reject_rate` `spread_strategy`: 1188/1188 rejects
- `medium` `zero_signal_strategy` `squeeze_setup`: 476 decisions, 0 signals
- `medium` `high_reject_rate` `squeeze_setup`: 476/476 rejects
- `medium` `zero_signal_strategy` `stop_hunt_detection`: 333 decisions, 0 signals
- `medium` `high_reject_rate` `stop_hunt_detection`: 333/333 rejects
- `medium` `zero_signal_strategy` `structure_break_retest`: 483 decisions, 0 signals
- `medium` `high_reject_rate` `structure_break_retest`: 483/483 rejects
- `medium` `zero_signal_strategy` `structure_pullback`: 1149 decisions, 0 signals
- `medium` `high_reject_rate` `structure_pullback`: 1149/1149 rejects
- `medium` `zero_signal_strategy` `supertrend_follow`: 1084 decisions, 0 signals
- `medium` `high_reject_rate` `supertrend_follow`: 1084/1084 rejects
- `medium` `zero_signal_strategy` `turtle_soup`: 222 decisions, 0 signals
- `medium` `high_reject_rate` `turtle_soup`: 222/222 rejects
- `medium` `zero_signal_strategy` `volume_anomaly`: 248 decisions, 0 signals
- `medium` `high_reject_rate` `volume_anomaly`: 248/248 rejects
- `medium` `zero_signal_strategy` `volume_climax_reversal`: 589 decisions, 0 signals
- `medium` `high_reject_rate` `volume_climax_reversal`: 589/589 rejects
- `medium` `zero_signal_strategy` `vwap_trend`: 1538 decisions, 0 signals
- `medium` `high_reject_rate` `vwap_trend`: 1538/1538 rejects
- `medium` `zero_signal_strategy` `whale_walls`: 613 decisions, 0 signals
- `medium` `high_reject_rate` `whale_walls`: 613/613 rejects
- `medium` `zero_signal_strategy` `wick_trap_reversal`: 602 decisions, 0 signals
- `medium` `high_reject_rate` `wick_trap_reversal`: 602/602 rejects
- `medium` `zero_signal_strategy` `wyckoff_spring`: 586 decisions, 0 signals
- `medium` `high_reject_rate` `wyckoff_spring`: 586/586 rejects
- `medium` `priority_asset_zero_signals` `BTCUSDT`: 620 rows, 0 signals
- `medium` `priority_asset_zero_signals` `ETHUSDT`: 603 rows, 0 signals
- `medium` `priority_asset_zero_signals` `XAUUSDT`: 551 rows, 0 signals
- `medium` `priority_asset_zero_signals` `XAGUSDT`: 569 rows, 0 signals

## Threshold Review Targets

- None.
