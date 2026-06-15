#!/usr/bin/env python3
"""Run mypy on CI-critical modules without pulling the entire bot graph."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# follow-imports=skip: isolated modules (no pydantic re-export needed).
SKIP_IMPORTS = (
    "bot/runtime/merge.py",
    "bot/engine/lanes.py",
    "engine/contract.py",
    "bot/delivery/formatting.py",
    "engine/domain/strategy_catalog.py",
    "bot/delivery/confluence.py",
)

# follow-imports=silent: follow pydantic/dotenv for config models only.
SILENT_IMPORTS = ("engine/domain/config.py",)


def _run_mypy(targets: tuple[str, ...], extra_args: list[str]) -> int:
    cmd = [
        sys.executable,
        "-m",
        "mypy",
        "--config-file",
        str(REPO_ROOT / "pyproject.toml"),
        *extra_args,
        *targets,
    ]
    print("[mypy]", " ".join(cmd), flush=True)
    return subprocess.call(cmd, cwd=REPO_ROOT)


def main() -> int:
    code = _run_mypy(SKIP_IMPORTS, ["--follow-imports=skip"])
    if code != 0:
        return code
    return _run_mypy(SILENT_IMPORTS, ["--follow-imports=silent"])


if __name__ == "__main__":
    raise SystemExit(main())
