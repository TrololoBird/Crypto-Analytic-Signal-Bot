#!/usr/bin/env python3
"""Import-smoke all declared runtime and optional dependencies."""

from __future__ import annotations

import importlib
import sys
from typing import NamedTuple

# Maps pyproject extra -> modules that must import when extra is installed
CORE_MODULES = (
    "dotenv",
    "polars",
    "aiohttp",
    "numpy",
    "aiogram",
    "websockets",
    "aiosqlite",
    "msgspec",
    "tenacity",
    "structlog",
    "pydantic",
)

LIVE_MODULES = (
    "fastapi",
    "uvicorn",
    "prometheus_client",
    "orjson",
)

DEV_MODULES = (
    "ruff",
    "mypy",
    "pre_commit",
)

TEST_MODULES = (
    "pytest",
    "pytest_asyncio",
    "pytest_cov",
)

REGIME_MODULES = (
    "sklearn",
    "statsmodels",
    "hmmlearn",
)


class Check(NamedTuple):
    module: str
    optional: bool


def _try_import(name: str) -> str | None:
    try:
        importlib.import_module(name)
        return None
    except ImportError as exc:
        return str(exc)


def _run_checks(label: str, modules: tuple[str, ...], *, optional: bool) -> list[str]:
    errors: list[str] = []
    for module in modules:
        err = _try_import(module)
        if err is None:
            print(f"  [OK] {module}")
            continue
        if optional:
            print(f"  [SKIP] {module} (not installed)")
            continue
        print(f"  [FAIL] {module}: {err}")
        errors.append(f"{label}:{module}: {err}")
    return errors


def main() -> int:
    errors: list[str] = []
    print("Core dependencies:")
    errors.extend(_run_checks("core", CORE_MODULES, optional=False))

    print("Live extras:")
    errors.extend(_run_checks("live", LIVE_MODULES, optional=True))

    print("Dev extras:")
    errors.extend(_run_checks("dev", DEV_MODULES, optional=True))

    print("Test extras:")
    errors.extend(_run_checks("test", TEST_MODULES, optional=True))

    print("Regime extras (optional):")
    errors.extend(_run_checks("regime", REGIME_MODULES, optional=True))

    # polars_ta / polars_ols naming
    for module in ("polars_ta", "polars_ols"):
        err = _try_import(module)
        if err is None:
            print(f"  [OK] {module}")
        else:
            print(f"  [SKIP] {module} (not installed)")

    if errors:
        print("DEPENDENCY CHECK FAILED:")
        for err in errors:
            print(f"  - {err}")
        return 1
    print("[OK] All required dependency imports succeeded")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
