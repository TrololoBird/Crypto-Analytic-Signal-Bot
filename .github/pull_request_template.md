## Summary

<!-- What changed and why (1–3 sentences). -->

## Type

- [ ] fix (bug / CI / ops)
- [ ] feat (strategy, market, delivery)
- [ ] refactor (no behavior change)
- [ ] docs / tooling only

## Checklist (agent runs before merge)

- [ ] `make check` (ruff + compileall + refactor gate)
- [ ] `pytest tests/ -q --ignore=tests/live` (or targeted tests noted below)
- [ ] Delivery path unchanged OR audited (`validate_signal_contract` → confluence → `deliver`)
- [ ] No secrets / `config.toml` / `.env` in diff

## Test plan

<!-- Commands run and results, e.g. make smoke, PYTEST_LIVE=1 … -->

## Backlog ID (if applicable)

<!-- e.g. OPS-*, V1.1-* from docs/DEFINITION_OF_DONE.md -->
