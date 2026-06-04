---
name: de-bloat
description: F12 structural splits for oversized modules (memory.py, pipeline.py, ws.py). Use when REFACTOR_PLAN de-bloat resumes and health audit shows files over 500 LOC.
model: inherit
readonly: false
is_background: false
---

## Targets (priority)

1. `bot/persistence/repository/memory.py`
2. `bot/runtime/analyzer/pipeline.py`
3. `bot/market/ws.py`

## Rules

- `python scripts/project_health_audit.py` before/after
- Re-export from package `__init__.py` to preserve imports
- No silent strategy disables
- `python -m compileall -q bot` + wave pytest after each split
- `make graphify-update` when done

Do not split files without functional gain (REFACTOR_PLAN §7).
