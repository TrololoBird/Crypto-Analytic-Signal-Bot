---
name: refactor-module
description: Guides safe deletion or rewrite of bloated bot modules during v9 refactor. Use when removing modules, splitting monoliths, or when the user asks to simplify the project structure per REFACTOR_PLAN.md.
---

# Refactor Module (v9)

Read `docs/REFACTOR_PLAN.md` before large changes.

## Decision tree

1. **Orphan / duplicate?** → Delete (grep imports + compileall)
2. **>500 LOC AI monolith?** → Rewrite slim module in target package, do not migrate line-by-line
3. **Core contract** (`contract.py`, `confluence`, `delivery_orchestrator`) → Refactor in place with live tests

## Target packages

| Old | New |
|-----|-----|
| `binance_client` + `ws_manager` + `market_data` | `bot/market/` |
| `features.py` + `features_*.py` | `bot/features/` |
| `application/*` | `bot/runtime/` |
| `tracking` + `outcomes` + `diary_store` | `bot/persistence/` |

## Per-step checklist

```
- [ ] graphify query "<module role>" (if graph exists)
- [ ] grep importers; update or remove
- [ ] python -m compileall -q bot
- [ ] validate_config
- [ ] PYTEST_LIVE=1 pytest tests/live/
- [ ] graphify update .
```

## Anti-patterns

- Do not add compatibility shims unless one release cycle requires it
- Do not preserve dead code "for later"
- Do not expand scope beyond the module under refactor

See `.Codex/rules/no-bloat.md`.
