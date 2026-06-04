---
name: de-bloat
description: F12 structural splits for oversized modules (memory.py, pipeline.py, ws.py). Use when REFACTOR_PLAN de-bloat resumes.
tools: Bash, Read, Write, Grep, Glob
---

## Targets (priority)

1. `bot/persistence/repository/memory.py` (~2073 LOC)
2. `bot/runtime/analyzer/pipeline.py` (~1554 LOC)
3. `bot/market/ws.py` (~1937 LOC)

## Rules

- `python scripts/project_health_audit.py` before/after
- Re-export from package `__init__.py` to avoid breaking imports
- No silent strategy disables
- `compileall` + wave pytest after each split
- `graphify update .` when done

Freeze arbitrary splits without functional gain (REFACTOR_PLAN §7).
