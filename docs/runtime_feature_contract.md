# Runtime feature contract (public)

## 1) Runtime call path and contract boundary

Confirmed runtime entry path:

1. `main.py`
2. `bot.cli.run()`
3. `bot.application.bot.SignalBot`

Public runtime import surface at package boundary is intentionally minimal:

- `bot.SignalBot`
- `bot.BotSettings`
- `bot.load_settings`

This surface is defined via `bot.__all__` and mirrored in `bot/runtime_contract.py::RUNTIME_PUBLIC_IMPORT_CONTRACT`, then covered by guard tests.

## 2) Public feature payload contract

Public feature schema source of truth:

- `bot/feature_contract.py`
- `PUBLIC_FEATURE_SCHEMA_VERSION`
- `PUBLIC_FEATURE_FIELDS`
- `validate_public_feature_payload()`
- `normalize_public_feature_payload()`

Rules:

- Producer payloads MUST contain exactly the keys from `PUBLIC_FEATURE_FIELDS`.
- Missing keys are rejected.
- Unknown/extra keys are rejected.
- Output payload ordering is stable and follows `PUBLIC_FEATURE_FIELDS` order.

## 3) Scaffold isolation policy

Scaffold/prototype modules must not appear on production runtime imports.

Current denylist checks include:

- `bot.telegram_bot`
- module names containing: `scaffold`, `experimental`, `prototype`

Guard checks live in `bot/runtime_contract.py` (`assert_runtime_call_path_is_clean`, `assert_runtime_import_contract`). Tests fail fast if blocked names appear on the runtime call path.

## 4) Extension policy (how to evolve safely)

When adding a new public feature field:

1. Add field to `PUBLIC_FEATURE_FIELDS` in `bot/feature_contract.py`.
2. Populate value in producer (`build_prepared_feature_snapshot`).
3. Keep schema exactness validation enabled (no permissive extras).
4. Bump `PUBLIC_FEATURE_SCHEMA_VERSION` only for explicit contract version change.
5. Update/add regression tests for both positive and negative paths.

Compatibility note:

- Uncoordinated growth of payload keys is considered a breaking change and must be blocked by tests.


## 5) Runtime contract extension checklist

When extending runtime surface intentionally:

1. Update `bot/runtime_contract.py` constants (`RUNTIME_CALL_PATH_FILES`, denylist, or public import tuple).
2. Keep `bot.__all__` aligned with `RUNTIME_PUBLIC_IMPORT_CONTRACT`.
3. Add/adjust both positive and negative contract tests in `tests/test_feature_contracts.py`.
4. Document the reason and blast radius in this file (and ADR if breaking).
