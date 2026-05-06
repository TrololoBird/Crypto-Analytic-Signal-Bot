# Phase 4 Tasks

Generated: 2026-05-06

## Execution Order

TASK-1: Verify registry, config, and endpoint boundaries.
Verification: import strategy count, config validation, endpoint-policy tests.

TASK-2: Fix BOS/CHoCH stop fallback regression coverage.
Verification: `tests/test_strategies.py` passes.

TASK-3: Fix telemetry analyzer scalability and schema handling.
Verification: analyzer completes on latest run and writes markdown/json reports.

TASK-4: Validate asset-fit routing behavior.
Verification: `tests/test_strategy_asset_fit.py` passes.

TASK-5: Run full regression and lint checks.
Verification: full `pytest` and full `ruff` before commit.

TASK-6: Commit and push all current project changes to `origin/main`.
Verification: `git status`, `git commit`, `git push origin main`.

## Dependency Notes

- TASK-2 and TASK-3 are independent.
- TASK-5 depends on all code/test/report updates.
- TASK-6 depends on successful local verification or explicitly documented validation failure.

## Not Completed By These Tasks

- A new 60-minute clean live validation after the final analyzer/test edits.
- Real Telegram delivery validation.
- A proof that priority-asset signal rate improved in fresh telemetry.
