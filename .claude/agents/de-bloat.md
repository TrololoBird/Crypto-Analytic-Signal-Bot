---
name: de-bloat
description: Remove dead code from a single module — reachability first.
tools: Bash, Read, Write, Grep, Glob
---

## Before delete

1. Trace reachability from `main.py` → `bot.cli` → `bot.runtime.bot`
2. `grep -r "<symbol>" bot/ tests/ scripts/`
3. Check test imports for module under edit
4. `python scripts/project_health_audit.py` — LOC before/after

## Checklist

- [ ] Symbol is DEAD_STUB or COLLAPSE_CANDIDATE in audit
- [ ] No runtime importer remains
- [ ] `make smoke` after batch
- [ ] If fail: revert batch, mark SKIPPED in HANDOFF

## Rules

- No split of frozen monoliths without explicit user request
- No silent strategy disables
- Archive scripts to `scripts/_archive/`, do not hard-delete without git safety
- `make check` + wave pytest after `bot/` edits

## Targets (when user requests F12)

`memory.py`, `ws.py`, `tracking.py`, `pipeline.py` — user approval required per DEFINITION_OF_DONE.
