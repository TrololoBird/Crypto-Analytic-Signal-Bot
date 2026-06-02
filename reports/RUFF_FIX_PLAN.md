# Ruff zero-errors plan (full project)

**Goal:** `ruff check .` → 0 violations. No `noqa`, no `per-file-ignores`, no rule `ignore` in `pyproject.toml`.

**Scope:** `bot/`, `tests/`, `scripts/`, `main.py`, root utilities.

**Baseline (2026-06-02):** 258 violations in `bot tests scripts main.py` (see `reports/ruff_full_remaining.json`).

## Wave 0 — Blockers

| Rule | Count | Action |
|------|------:|--------|
| invalid-syntax | 0–6 | `compileall`; fix broken files first |
| E501 | 1 | Line wrap |
| I001 | 1 | `ruff check --fix --select I001` |

## Wave 1 — Quick auto-fix

| Rule | Count | Action |
|------|------:|--------|
| I001, UP*, RET*, SIM114, etc. | ~5 | `ruff check --fix --unsafe-fixes` |

## Wave 2 — Mechanical refactors (parallel subagents)

| Wave | Rules | Count | Fix strategy |
|------|-------|------:|--------------|
| 2a | TRY401 | 35 | Use `logger.error("msg", exc_info=exc)` instead of `logger.exception` where TRY401 flags |
| 2b | PERF401 | 29 | Replace `for x in y: list.append` with `list.extend` / comprehension |
| 2c | PLW0127 | 22 | Remove self-assignment (`x = x`) or use distinct names |
| 2d | SIM102 | 16 | Merge nested `if` with `and` |
| 2e | TRY300 | 23 | Add `else` branch or restructure try/except |
| 2f | PLW2901 | 6 | Rename loop variables that shadow outer names |

## Wave 3 — API / typing style (requires signature care)

| Rule | Count | Fix strategy |
|------|------:|--------------|
| FBT001/002/003 | 48 | Keyword-only bools: `*, flag: bool = False` or `StrEnum` / `Literal` instead of bare `bool` positional |
| PLW0603 | 7 | Remove `global` in scripts; module-level imports + `main()` bootstrap only |
| PLW1510 | 5 | `subprocess.run(..., check=True)` or explicit return-code handling |

## Wave 4 — Async / time / paths

| Rule | Count | Fix strategy |
|------|------:|--------------|
| ASYNC109/110/220/240 | 15 | `asyncio.timeout`, `asyncio.sleep`, thread pool for blocking I/O |
| DTZ005/007/011 | 8 | `datetime.now(tz=UTC)`, timezone-aware strptime |
| PTH105/109/123 | 4 | `Path.read_text`, `Path.cwd`, `Path.open` |
| RUF006 | 4 | Store `asyncio.create_task` refs; cancel on shutdown |
| B904 | 3 | `raise X from exc` in except blocks |

## Wave 5 — Unicode / docs / typing hygiene

| Rule | Count | Fix strategy |
|------|------:|--------------|
| RUF001/002/003 | 21 | Latin transliteration in code strings OR `\uXXXX` escapes; keep RU in `.md` only |
| PGH003 | 4 | Replace blanket `# type: ignore` with specific codes or proper types |
| PLE0604 | 3 | Fix `__all__` to list only strings |
| G201/G101 | 2 | Logging `exc_info` / `extra` key naming |

## Verification (agent runs all)

```powershell
.\.venv\Scripts\ruff.exe check .
.\.venv\Scripts\ruff.exe format --check bot tests scripts main.py
.\.venv\Scripts\python.exe scripts\fix_py314_except.py bot tests scripts main.py
.\.venv\Scripts\python.exe -m compileall -q bot tests scripts
.\.venv\Scripts\python.exe scripts\verify_refactor_gate.py
```

## CI alignment

Update `.github/workflows/ci.yml` lint step to `ruff check .` (full tree, not only `bot/ tests/`).
