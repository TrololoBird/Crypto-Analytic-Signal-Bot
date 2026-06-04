# Verify after code changes

Run the full agent-owned verification sequence. Do not ask the user to run commands.

## Steps

1. `source .venv/bin/activate` (create venv with Python 3.14 if missing)
2. `python -m compileall -q bot`
3. `python scripts/validate_config.py --config config.toml` (or `config.toml.example` if no local config)
4. `python scripts/verify_refactor_gate.py`
5. `pytest tests/test_wave_f9_agent_*.py tests/test_wave_f10_agent_*.py tests/test_wave_f11_*.py -q`
6. If Binance REST reachable (`python scripts/probe_binance_access.py --all-configured`):
   - `PYTEST_LIVE=1 pytest tests/live/ -v`
   - `python scripts/live_check_pipeline.py --symbols BTCUSDT --limit 1`
7. `make graphify-update` if graphify CLI is installed

## Report

- Pass/fail per step with last 20 lines of any failure
- Whether proxy/network blocked live tests (not a code regression)
- Files changed in this session

Use skill `live-binance-verify` when market/features/runtime changed.
