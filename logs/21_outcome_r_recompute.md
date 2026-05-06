# Outcome R-Multiple Recompute

Generated: 2026-05-04T12:05:00+00:00

## Evidence Boundary

- Confirmed: recompute uses persisted `signal_outcomes` joined to `active_signals.initial_stop` by `tracking_id`.
- Confirmed: `--apply` creates a timestamped SQLite backup before updating rows.
- Inference: `initial_stop` is treated as the original planned risk anchor for R-multiple analytics.

## Summary

- Applied: True
- DB: C:\Users\undea\Documents\bot2\data\bot\bot.db
- Backup: C:\Users\undea\Documents\bot2\data\bot\bot.db.backup_r_recompute_20260504_120459
- Rows scanned: 78
- Rows recomputable: 78
- Rows changed: 30
- Max absolute R delta: 237.0043

## Largest Changes

| Tracking ID | Symbol | Setup | Result | Old R | New R | Old Max Loss % | New Max Loss % |
| --- | --- | --- | --- | ---: | ---: | ---: | ---: |
| LINKUSDT|cvd_divergence|long|20260504T063121634614Z | LINKUSDT | cvd_divergence | tp2_hit | 239.3662 | 2.3619 | 0.0051 | -0.5160 |
| 1000LUNCUSDT|liquidation_heatmap|long|20260504T045702753376Z | 1000LUNCUSDT | liquidation_heatmap | tp2_hit | 137.2734 | 1.9572 | -0.0459 | -3.2206 |
| ONDOUSDT|cvd_divergence|long|20260504T003747466048Z | ONDOUSDT | cvd_divergence | tp2_hit | 41.2518 | 2.9073 | 0.0494 | -0.7015 |
| DOGEUSDT|liquidation_heatmap|long|20260504T010028597826Z | DOGEUSDT | liquidation_heatmap | tp2_hit | 38.4556 | 1.8553 | -0.0459 | -0.9509 |
| BIOUSDT|structure_pullback|long|20260501T114528315077Z | BIOUSDT | structure_pullback | expired | 35.5000 | 0.5510 | 0.0502 | -3.2344 |
| ZECUSDT|ls_ratio_extreme|long|20260504T003741396028Z | ZECUSDT | ls_ratio_extreme | tp2_hit | 32.5545 | 2.2030 | 0.0801 | -1.1839 |
| ZECUSDT|liquidation_heatmap|long|20260504T003741469920Z | ZECUSDT | liquidation_heatmap | tp2_hit | 32.5545 | 2.2030 | 0.0801 | -1.1839 |
| BZUSDT|ls_ratio_extreme|long|20260504T063037922859Z | BZUSDT | ls_ratio_extreme | tp2_hit | 29.2000 | 2.2324 | 0.0464 | -0.6070 |
| LINKUSDT|volume_anomaly|long|20260504T090141392605Z | LINKUSDT | volume_anomaly | tp2_hit | 25.7500 | 2.2697 | 0.0426 | -0.4832 |
| LINKUSDT|price_velocity|long|20260504T090140873054Z | LINKUSDT | price_velocity | tp2_hit | 25.0000 | 2.2893 | 0.0426 | -0.4651 |
| ZBTUSDT|bos_choch|long|20260501T131958888420Z | ZBTUSDT | bos_choch | stop_loss | -1.0000 | -0.0049 | -0.0158 | -3.2172 |
| BRUSDT|liquidation_heatmap|long|20260504T060253221159Z | BRUSDT | liquidation_heatmap | stop_loss | 1.0000 | 0.0682 | 0.1360 | -1.9951 |
| DASHUSDT|liquidation_heatmap|long|20260504T064634114792Z | DASHUSDT | liquidation_heatmap | stop_loss | -1.0000 | -0.0958 | -0.2073 | -2.1641 |
| DASHUSDT|liquidation_heatmap|long|20260504T045358867263Z | DASHUSDT | liquidation_heatmap | stop_loss | -1.0000 | -0.0964 | -0.2124 | -2.2048 |
| ORDIUSDT|liquidation_heatmap|long|20260504T071244466872Z | ORDIUSDT | liquidation_heatmap | stop_loss | 1.0000 | 0.2063 | 0.1845 | -0.8940 |
| APTUSDT|wick_trap_reversal|long|20260502T022040213013Z | APTUSDT | wick_trap_reversal | stop_loss | -1.0030 | -1.0030 | -1.6097 | -1.6048 |
| NEARUSDT|multi_tf_trend|short|20260504T090010846193Z | NEARUSDT | multi_tf_trend | stop_loss | -1.0709 | -1.0709 | -0.7862 | -0.7341 |
| APTUSDT|cvd_divergence|long|20260501T111522642761Z | APTUSDT | cvd_divergence | stop_loss | -1.0025 | -1.0025 | -0.5240 | -0.5226 |
| BABYUSDT|liquidation_heatmap|long|20260504T062324541889Z | BABYUSDT | liquidation_heatmap | stop_loss | -1.0227 | -1.0227 | -1.4230 | -1.3913 |
| RAVEUSDT|bos_choch|short|20260502T022649982526Z | RAVEUSDT | bos_choch | stop_loss | -1.0069 | -1.0069 | -0.7862 | -0.7808 |
