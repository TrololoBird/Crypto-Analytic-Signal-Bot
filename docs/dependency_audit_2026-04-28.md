# Static dependency audit (2026-04-28)

## Scope
- `pyproject.toml`
- `requirements.txt`
- `requirements-modern.txt`
- `requirements-frozen.txt`
- `requirements-optional.txt`
- `requirements-dev-live.txt`
- `uv.lock`

## 1) Version-range conflicts across declared dependency files

### Confirmed
- Runtime dependency set in `pyproject.toml` and `requirements.txt` is aligned 1:1 by package and constraints.
- `requirements-modern.txt` is identical to `requirements.txt`.
- `requirements-frozen.txt` pins all runtime dependencies to exact versions that satisfy `pyproject.toml` constraints.
- Optional/dev/live dependency ranges in `pyproject.toml` extras match `requirements-optional.txt` and `requirements-dev-live.txt` package versions.

### Notes
- `uvicorn[standard]>=0.46.0` in `pyproject.toml` is represented as `uvicorn>=0.46.0` in requirements files (extras omitted). This is not a range conflict, but it changes the resolved transitive set.
- `polars_ta` appears as `polars-ta` in requirement files (normalized project name). This is expected normalization, not a conflict.

## 2) Potential Windows risk packages (native deps / wheel availability / build backend)

### Higher risk (native toolchain often required when wheels are missing)
- `lightgbm`
- `xgboost`
- `hmmlearn`
- `scikit-learn`

### Medium risk (binary extensions; usually wheels exist, but wheel lag can affect latest Python)
- `numpy`
- `polars`
- `aiohttp` (C extensions and compiled dependencies)
- `orjson`
- `msgspec`

### Operational caveat
- `uvicorn[standard]` may pull optional acceleration packages whose platform coverage differs by OS/Python; behavior on Windows can differ from Linux defaults.

## 3) Lock-file compliance with declared constraints

### Critical findings
- `uv.lock` is effectively empty (only header keys) and does not lock any declared package from `pyproject.toml`.
- `pyproject.toml` sets `requires-python = ">=3.13,<3.14"`, while `uv.lock` header says `requires-python = ">=3.13"`. This is broader and not equivalent.

### Impact
- Reproducible installs via `uv sync --frozen` are not guaranteed.
- CI/dev machines may resolve different dependency graphs, especially over time.

## 4) Version-drift risk between dependency files

### Current state
- Low current drift for runtime: `pyproject.toml`, `requirements.txt`, `requirements-modern.txt`, and `requirements-frozen.txt` are currently synchronized for direct runtime dependencies.

### Drift risks
- Dual maintenance of multiple requirement files creates medium future drift risk.
- Extras mismatch risk exists because `uvicorn[standard]` is not mirrored with extras in requirements files.
- Missing populated lock file creates high drift risk for transitive dependencies.

## 5) Pin/constraint fixes with priority

### Critical
1. Regenerate and commit a fully populated `uv.lock` from current constraints (including extras used in CI/runtime profiles).
2. Make lock Python constraint consistent with project constraint (`>=3.13,<3.14`).
3. Enforce lock freshness in CI (`uv lock --check` or equivalent frozen sync).

### Medium
1. Decide and standardize whether `uvicorn[standard]` extras are required in requirements mirrors; align `requirements-optional.txt` and `requirements-dev-live.txt` with project intent.
2. Add upper bounds for native-heavy optional ML stack to reduce surprise breakage on new upstream releases.
3. Split optional heavy ML dependencies into a dedicated constraints file for Windows if needed.

### Improvement
1. Document one source of truth policy (prefer `pyproject.toml` + generated lock/exports).
2. Auto-generate `requirements*.txt` from `pyproject.toml`/lock to avoid manual drift.
3. Add a lightweight dependency consistency check script to CI (name + spec diff across files).

## Confidence
- Confirmed facts are based on static file inspection only.
- Windows wheel/build risk categorization is heuristic and should be validated against target Python/arch in CI.
