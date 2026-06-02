# Ruff — full project zero-errors pass

**Config:** `pyproject.toml` — full rule set (E, F, W, I, UP, B, SIM, RUF, ARG, TRY, ASYNC, BLE, …), **no** `ignore`, **no** `per-file-ignores`.

**Scope:** `bot/`, `tests/`, `scripts/`, `main.py` (excludes `.git`, `.venv`, `data/`, `graphify-out/`).

## Status: PASS

```powershell
.\.venv\Scripts\ruff.exe check bot tests scripts main.py
# All checks passed!

.\.venv\Scripts\ruff.exe check .
# All checks passed!  (no .py outside scope except excluded dirs)
```

## Progress

| Stage | Violations |
|-------|----------:|
| Baseline extended scan | ~2941 |
| After waves 1–5 (BLE, E501, E402, ARG, RUF012, PLC0415, FBT, ASYNC, …) | ~258 |
| After auto-fix + __all__ / F841 cleanup | **0** |

## Artifacts

| File | Purpose |
|------|---------|
| [RUFF_FIX_PLAN.md](RUFF_FIX_PLAN.md) | Wave plan |
| [ruff_inventory.json](ruff_inventory.json) | Historical baseline JSON |
| [ruff_full_remaining.json](ruff_full_remaining.json) | Mid-pass snapshot |

## CI

`.github/workflows/ci.yml` lint job: `ruff check bot/ tests/ scripts/ main.py` + format + `fix_py314_except.py`.
