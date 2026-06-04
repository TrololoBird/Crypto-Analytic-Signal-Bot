# Fix and verify (debug loop)

For bugs/regressions. Agent closes the loop with evidence.

## Steps

1. Reproduce: logs, test, or `live_check_*` as appropriate
2. Root cause in `bot/` — read before edit
3. Minimal fix (no gate weakening, no silent strategy disable)
4. Run:
   ```bash
   source .venv/bin/activate
   python -m compileall -q bot
   pytest tests/test_wave_f9_agent_*.py tests/test_wave_f10_agent_*.py -q
   ```
5. Targeted test for the bug if exists; else add none unless user asked
6. If delivery-related: delegate `delivery-guardian` readonly audit

## Report

- Root cause (1 paragraph)
- Fix (files)
- Evidence (command output snippet)
- What was **not** changed and why
