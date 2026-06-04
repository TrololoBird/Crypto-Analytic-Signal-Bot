# Calibrate from live_watch run

Post-session calibration using F11 bridge tooling.

## Input

User provides `run_id` (e.g. `20260604T014627Z`) or you discover latest under `data/live_watch/`.

## Steps

1. `source .venv/bin/activate`
2. Resolve run: `python -c "from bot.diagnostics.live_watch import ..."` or list `data/live_watch/`
3. `python scripts/live_watch_rollup_report.py --config config.toml`
4. `python scripts/calibration_pipeline.py --run-id <RUN_ID> --config config.toml`
5. If REST OK: `python scripts/strategy_shortlist_matrix.py --run-id <RUN_ID> --config config.toml --live-shortlist`
6. Review weighted confluence telemetry before toggling `use_weighted_confluence`

## Output

- Top strategies by hit rate / reject stage
- Recommended threshold changes (cite telemetry, do not silent-disable)
- Whether to enable `use_weighted_confluence`

Skill: `calibration-wave`
