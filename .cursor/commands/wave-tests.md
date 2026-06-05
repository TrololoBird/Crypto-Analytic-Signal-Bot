# Wave regression tests (F9–F11)

Fast offline regression for agent waves — no Binance network.

```bash
source .venv/bin/activate
python -m compileall -q bot
python scripts/verify_refactor_gate.py
pytest tests/test_wave_f9_agent_*.py tests/test_wave_f10_agent_*.py tests/test_wave_i_calibration.py -q
```

Report total passed/failed and first failure traceback.
