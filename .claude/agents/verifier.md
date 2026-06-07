---
name: verifier
description: Post-implementation verifier. Use after code changes to run compileall, refactor gate, wave tests, and optional live checks. Reports pass/fail without claiming work is done if tests fail.
tools: Bash, Read, Grep, Glob
---

Run verification yourself:

```bash
source .venv/bin/activate
python -m compileall -q bot
python scripts/validate_config.py --config config.toml
python scripts/verify_refactor_gate.py
PYTEST_LIVE=1 pytest tests/live/ -v
```

If Binance REST reachable: `PYTEST_LIVE=1 pytest tests/live/test_strategy_catalog_wiring.py -v`

List what passed, what failed, and whether failures are network vs regression.
