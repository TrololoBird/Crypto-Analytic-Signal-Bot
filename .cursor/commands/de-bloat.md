# F12 de-bloat one module

Structural split per REFACTOR_PLAN Phase 3 checkpoint — one module per session.

## Pick target (default order)

1. `bot/persistence/repository/memory.py`
2. `bot/runtime/analyzer/pipeline.py`
3. `bot/market/ws.py`

## Process

1. `python scripts/project_health_audit.py --stale-days 2` — note LOC
2. Skill `refactor-module` — no silent strategy disable
3. Extract cohesive query/handler groups; keep imports stable via re-exports
4. `python -m compileall -q bot` + wave pytest suite
5. `make graphify-update`

## Rules

- Do not add file splits without functional gain
- Preserve delivery path and public-only Binance boundary
- Subagent `de-bloat` (Claude Code) when Task limits allow

Report: before/after LOC, files created, tests run.
