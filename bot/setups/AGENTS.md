# Setup Helpers Agent

## Scope

Work in `bot/setups/`.

## Role

These files hold shared utilities used across multiple strategies. A small
change here can affect many setups at once.

`bot/setups/__init__.py` is the active shared-helper module imported as
`bot.setups`. Do not recreate the removed sibling `bot/setups.py` legacy file.

## Read Before Editing

- The touched helper
- At least one current strategy caller
- `bot/domain/schemas.py` if signal shape or prepared-symbol expectations are involved
- `bot/setup_base.py` if strategy-engine adapter behavior is involved

## Local Rules

- Prefer changing shared helpers here over copy-pasting logic into individual strategies.
- Preserve Polars-native behavior and guard against empty frames, missing columns, and invalid numeric values.
- Keep helper outputs stable and explicit. If a helper contract changes, update all known callers in the same change.
- Reuse existing helper boundaries for score building, rejection logging, and SL/TP construction unless the task requires a real API change.

## Token Discipline

- Search for helper call sites with `rg` before editing; this directory has broad blast radius.
- Read only the helper implementation and representative callers, not every strategy.

## Verification

- After changing a shared helper, verify at least the direct callers you touched or affected.
- Prefer compile/import checks, local strategy diagnostics, and telemetry
  reason review. Generated tests are supplemental only.
