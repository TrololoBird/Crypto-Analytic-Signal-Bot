---
name: verify-after-change
description: Runs compileall, refactor gate, and wave F9-F11 pytest after code edits. Use automatically after implementing features, fixes, or refactors in bot/ or scripts/.
---

```bash
source .venv/bin/activate
python -m compileall -q bot
python scripts/validate_config.py --config config.toml
python scripts/verify_refactor_gate.py
pytest tests/test_wave_f9_agent_*.py tests/test_wave_f10_agent_*.py tests/test_wave_f11_*.py -q
```

Report pass/fail with last lines of any failure. Do not claim done if tests fail.
