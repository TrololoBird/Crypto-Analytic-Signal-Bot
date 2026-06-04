---
name: calibration-wave
description: Post-live calibration using calibration_pipeline, strategy_shortlist_matrix, and live_watch run_id bridge. Use after supervised sessions or when tuning strategy thresholds from telemetry.
---

# Calibration Wave

## Inputs

- `run_id` from `data/live_watch/` (F11 bridge)
- `config.toml` validated

## Pipeline

```bash
source .venv/bin/activate
python scripts/live_watch_rollup_report.py --config config.toml
python scripts/calibration_pipeline.py --run-id <RUN_ID> --config config.toml
```

When Binance REST reachable:

```bash
python scripts/strategy_shortlist_matrix.py --run-id <RUN_ID> --config config.toml --live-shortlist
```

Nightly:

```bash
make nightly-calibration
make shortlist-matrix
```

## Weighted confluence

Review confluence telemetry before enabling `use_weighted_confluence` in config (F9-S8).

## Rules

- Cite rejection reasons from telemetry JSONL / rollup
- Do not bypass `hard_confluence_gate`
- Do not silent-disable strategies
