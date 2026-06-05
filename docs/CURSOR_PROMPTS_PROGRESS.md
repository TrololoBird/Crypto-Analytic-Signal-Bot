# CURSOR_PROMPTS.md — progress tracker

Source: `/Users/tonyaleksandrov/Downloads/CURSOR_PROMPTS.md`  
Last updated: 2026-06-04 (re-verified + strategy_pools fix)

## Status: CLOSED — нет незакрытых промптов

**CURSOR_PROMPTS.md главнее** остальных инструкций проекта для этой программы работ.

Источник: `~/Downloads/CURSOR_PROMPTS.md` (647 строк).  
**Итоговый отчёт:** [CURSOR_PROMPTS_COMPLETION_REPORT.md](CURSOR_PROMPTS_COMPLETION_REPORT.md)

Все пункты из **Порядка выполнения** (Batch 1 → 2 → 3 + контрольные метрики) выполнены.  
Финальная верификация **2026-06-04**: `make check` OK, **409 passed** (`pytest --ignore=tests/live`).

---

## Batch 1 — Critical bugs ✅

| ID | Notes |
|----|-------|
| 1.1 | Lazy imports: `config.validate_for_runtime`, `health_manager.assess_radar_store`; B/C via `TYPE_CHECKING`. |
| 1.2 | `analyzer_ops.py` duplicate `persistence.repository` import removed. |
| 1.3 | `common.py` → `_common.py`; 4 strategies updated; `common.py` deleted. |

## Batch 2 — Cleanup ✅

| ID | Notes |
|----|-------|
| 2.1 | `bot/market/rest.py` deleted; imports → `rest_impl`. |
| 2.2 | `scripts/_archive/` + `README.md`; CI scripts kept (`fix_py314_except`, `project_health_audit`). |
| 2.3 | Audit in `_audit_scratch.txt`; **10** redundant wave tests deleted. |

## Batch 3 — Claude Code Pro ✅

| ID | Notes |
|----|-------|
| 3.1 | `CLAUDE.md` (299 lines) + `CLAUDE.md.bak`. |
| 3.2 | `.claude/` synced (agents, rules, skills, `no-bloat.md`). |
| 3.3 | `.claudeignore`, `docs/CLAUDE_QUICK_REF.md`, `PROJECT_MAP.md`. |
| 3.4 | Pre-push cycle hook; Makefile targets; pytest `slow` marker. |

## Post-batch ✅

| Task | Result |
|------|--------|
| Unit tests | **409 passed** (`pytest --ignore=tests/live`) |
| Imports / cycles | `make check-imports`, `make check-cycles` OK |
| `bot/**/*.py` count | **192** |
| `CLAUDE.md` | **299** lines |

## Follow-up sync (this session) ✅

Stale references after wave deletions and script archive:

| Area | Change |
|------|--------|
| `.cursor/` | `delivery-audit`, `delivery-guardian`, `verify`, `wave-tests`, `verifier` → `test_delivery_mtf_gate` / `test_wave_i_calibration` |
| `.claude/` | Same wave-test command updates |
| `CLAUDE.md`, `AGENTS.md`, `DEFINITION_OF_DONE`, `PROJECT_MAP`, roadmap docs | F11 → `test_wave_i_calibration` |
| `docs/PY_*`, `BOT_PACKAGES` | Archived script paths `scripts/_archive/` |
| `Makefile` | `check` uses `.venv`, includes `check-imports` + `check-cycles`; `lint --fix`; `unit-smoke` target |
| `.pre-commit-config.yaml` | Cycle hook → `.venv/bin/python` |

## Explicitly out of scope (per CURSOR_PROMPTS.md)

- `bot/runtime/bot.py` god-object refactor
- Protocol/ABC removal
- Per-strategy refactors (38 strategies)
- `bot/static/` frontend
- `PROJECT_AUDIT.md` (historical snapshot; not updated)

## Deleted wave tests (audit 2.3)

`test_wave_e2_hard_gate`, `e4_analytics`, `e7_analytics`, `e8_agent_c`, `e8_agent_f`, `f10_agent_s`, `f11_live_watch_bridge`, `f9_agent_n`, `f9_agent_q`, `f9_agent_s`

## Re-verification (2026-06-04, latest)

| Metric (из промпта) | Значение |
|---------------------|----------|
| `find bot/ -name "*.py" \| wc -l` | **192** |
| `import bot.runtime.bot` | **OK** |
| `pytest -q --ignore=tests/live` | **409 passed** |
| `wc -l CLAUDE.md` | **299** (<600) |
| `make check` | OK (gate + imports + cycles + 38 strategies) |

| Prompt 3.4 checklist | Статус |
|---------------------|--------|
| pre-commit: ruff `--fix` | ✅ |
| pre-commit: mypy `^bot/` | ✅ |
| pre-commit: check-circular-imports (pre-push) | ✅ `scripts/check_circular_imports.py` |
| Makefile: check-imports, check-cycles, lint, typecheck, unit-smoke | ✅ |
| pyproject: ruff line-length 100, mypy ignore_missing_imports, markers slow+live | ✅ |

Доп. фикс вне списка промптов: `asset_strategy_allowlist` — порядок из config (`strategy_pools.py`).

## Verification commands

```bash
make check
make unit-smoke
.venv/bin/pytest -q --ignore=tests/live
```

## Next work (outside CURSOR_PROMPTS.md)

No remaining prompts in the source file. Optional backlog: F12 de-bloat (`bot/runtime/bot.py`, `memory.py`), live `PYTEST_LIVE=1`, strategy calibration.
