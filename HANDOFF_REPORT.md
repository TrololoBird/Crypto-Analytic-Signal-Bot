# HANDOFF_REPORT — 2026-06-04

## Session summary

- Files before: 192 | Files after: 185 | Delta: −7
- Tests before: 409 passed | Tests after: 409 passed
- Pipeline bugs fixed: 6 | Pipeline bugs pending: 0

## What was done

1. Phase 0 baseline → `_master_refactor_baseline.txt` (192 files, 409 tests)
2. Phase 1 audit → `_master_audit_report.md`, `_stubs.txt`, `_logic_violations.txt`
3. Deleted dead shims → `bot/alerts.py`, `bot/strategies/catalog_spec.py`, `bot/strategies/spec_patterns.py`
4. Deleted dead module → `bot/backtest/` (4 `.py` + empty dir)
5. Archived 8 orphan scripts → `scripts/_archive/` (prior session)
6. Phase 2D → 10 duplicate wave tests already removed
7. Phase 2B DONE → collapsed pure-delegation barrels: `bot/features/__init__.py`, `bot/runtime/__init__.py`, `bot/ops/__init__.py`, `bot/persistence/repository/__init__.py`; 38 importers redirected
8. Fixed pipeline → `bot/strategies/_common.py` Wilder ATR; `delivery_orchestrator.py` contract guard; `dashboard/app.py` to_dicts
9. Rewrote `CLAUDE.md` strict template (106 lines); backup `CLAUDE.md.prev`
10. Cleaned `.claude/rules/` — removed Cursor-only rules; merged into `project-core.md`
11. OPS-2 → `use_weighted_confluence = true`; review `reports/ops2_weighted_confluence_review.json`
12. Completion gate → `make check`, 409 pytest, circular imports OK

## Pipeline bugs (pending, from Phase 3)

None.

## Dead stubs still in code (LIVE_STUBs from Phase 1A)

| file:line | what it should do |
|-----------|-------------------|
| bot/setups/spec_runtime.py:156 | `SpecDetectorSetup` subclass must wire `detect_setup`; raises if missing (fail-fast guard) |

## Logic violations found (Phase 1D)

| file:line | hit | verdict |
|-----------|-----|---------|
| bot/logging_config.py:12 | `api_key` in SENSITIVE_FIELDS | security redaction — keep |
| bot/market/data.py:45,228 | `api_key` in forbidden REST params | public-only guard — keep |

## What Claude should focus on next

1. OPS-1: post-harvest rollup + strategy redesign notes when 120m capture completes
2. OPS-3: `make nightly-calibration` when REST stable
3. Re-evaluate `action_min_score` vs harvest confluence distribution
4. Monitor `weighted_confluence_bridge_pass` in delivery telemetry
5. ~~Do not collapse `__init__.py` barrels~~ — done 2026-06-04 per explicit architect OK

## Files Claude must read before any change

1. `CLAUDE.md`
2. `HANDOFF_REPORT.md`
3. `docs/DEFINITION_OF_DONE.md`
4. `bot/runtime/bot.py`
5. `bot/runtime/delivery_orchestrator.py`
6. `bot/delivery/contract.py`
7. `bot/domain/schemas.py`
8. `bot/delivery/confluence.py`

## What Claude must NOT do in this project

1. Never place orders, use trading APIs, or add account authentication.
2. Never bypass `validate_signal_contract` → hard confluence gate → `delivery.deliver`.
3. Never import forbidden legacy paths; strategies only under `bot/strategies/`.
4. Never use `shift(-N)` or pandas on live signal paths.
5. Never put LLM inference on the hot path.
6. Never create ABC/Protocol/factory layers without architect approval.
7. Never split modules under 500 LOC or frozen monoliths without explicit request.
8. Never generate test files unless explicitly requested.
9. Never modify `bot/static/`.
10. Never disable strategies silently.
11. Never generate new «50 improvements» backlogs.

## Test commands

```bash
make smoke         # offline pytest
make check         # full gate
make live-smoke    # requires Binance access
make validate-config
python scripts/check_circular_imports.py
```

## Completion gate (final)

| Check | Result |
|-------|--------|
| `make check` | OK |
| `pytest --ignore=tests/live` | 409 passed |
| `check_circular_imports.py` | OK (78 modules) |
| `wc -l CLAUDE.md` | ≤400 |
| `find bot/ -name "*.py" \| wc -l` | 185 |
