# Supervised 6h live session

Long supervised run for signal quality and calibration data.

## Prep (agent executes)

1. `source .venv/bin/activate`
2. `python scripts/clean_session_data.py --mode smoke --config config.toml`
3. `python scripts/discover_binance_proxies.py` if REST geo-blocked
4. `python scripts/validate_config.py --config config.toml`

## Run

```bash
python -m scripts.live_supervised_session --hours 6 --minutes 360 --snapshot-interval 60 --takeover
```

`Await` until exit. Reap terminals if backgrounded.

## Post-session

1. `python scripts/live_watch_rollup_report.py --config config.toml`
2. Note `run_id` under `data/live_watch/`
3. `python scripts/calibration_pipeline.py --run-id <RUN_ID> --config config.toml`
4. Optional: `python scripts/strategy_shortlist_matrix.py --run-id <RUN_ID> --config config.toml`

Report: delivered count, top rejection reasons, proxy health.

Skill: `supervised-live-session`, then `calibration-wave`.
