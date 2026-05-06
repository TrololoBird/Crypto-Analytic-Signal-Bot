# Setup Score Recompute

Generated: 2026-05-04T12:08:38+00:00

## Evidence Boundary

- Confirmed: `setup_scores.outcome_window` was read from `data/bot/bot.db`.
- Confirmed: a backup was created before update: `data/bot/bot.db.backup_setup_scores_20260504_120838`.
- Confirmed: the recompute used the updated policy of minimum 8 outcomes, win reasons `tp1_hit`, `tp2_hit`, and `smart_exit`, low win-rate threshold 40%, penalty -0.05.

## Changes

| Setup | Prior Adjustment | New Adjustment | Window Size |
| --- | ---: | ---: | ---: |
| bos_choch | 0.0 | -0.05 | 12 |
| structure_pullback | 0.0 | -0.05 | 12 |
| spread_strategy | 0.0 | -0.05 | 13 |

## Resulting Runtime Effect

The performance guard now suppresses setups with a negative score adjustment. Existing poor-outcome windows therefore suppress `bos_choch`, `structure_pullback`, `spread_strategy`, and the already-penalized `liquidation_heatmap` until future outcome windows recover.
