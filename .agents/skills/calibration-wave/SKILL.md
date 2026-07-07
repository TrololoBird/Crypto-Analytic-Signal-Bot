---
name: calibration-wave
description: Post-live_watch calibration using calibration_pipeline and strategy_shortlist_matrix with run_id. Use after supervised sessions or when tuning strategy thresholds from telemetry.
---

```bash
source .venv/bin/activate
python scripts/live_watch_rollup_report.py --config config.toml
python scripts/calibration_pipeline.py --run-id <RUN_ID> --config config.toml
```

REST OK: add `strategy_shortlist_matrix.py --run-id <RUN_ID> --live-shortlist`

Cite telemetry for threshold changes. Never bypass confluence gate.
