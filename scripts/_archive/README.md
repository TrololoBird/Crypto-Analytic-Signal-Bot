# Archived one-off scripts

Moved here per refactor audits — not used on runtime or CI paths.

| Script | Purpose |
|--------|---------|
| `consolidate_all_modules.py` | Historical module merge tool |
| `consolidate_bot_modules.py` | Historical bot merge tool |
| `generate_py_audit_5x.py` | Legacy audit doc generator |
| `audit_py_deep_findings.py` | Legacy per-file audit generator |
| `check_scripts_readme.py` | Meta README checker |
| `run_30min_test.bat` | Windows-only smoke bat |
| `audit_wave_ef_tests.py` | Wave E/F test duplicate audit (MASTER_REFACTOR) |
| `check_db_schema.py` | Standalone DB schema check |
| `live_run_watchdog.py` | Legacy live watchdog |
| `live_session_status.py` | Legacy session status |
| `run_mypy_bot.py` | Superseded by `run_mypy_critical.py` |
| `run_until_msk.py` | One-off timezone runner |
| `recompute_outcome_r_multiples.py` | One-off outcome R recompute |
| `telemetry_analyzer.py` | Offline telemetry JSONL analysis |

**Still active (not archived):** `fix_py314_except.py`, `project_health_audit.py`, `common.py`, `smoke_fail_fast.py` (CI/Makefile/live paths).

Run archived tools explicitly if needed:

```bash
.venv/bin/python scripts/_archive/telemetry_analyzer.py
```
