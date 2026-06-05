#!/usr/bin/env python3
"""Run mypy on the full bot package (incremental strict adoption).

CI gates delivery-critical modules via ``scripts/run_mypy_critical.py``.
This script is for local/optional full-tree reporting.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    cmd = [
        sys.executable,
        "-m",
        "mypy",
        "bot/",
        "--config-file",
        str(REPO_ROOT / "pyproject.toml"),
    ]
    print("[mypy]", " ".join(cmd), flush=True)
    return subprocess.call(cmd, cwd=REPO_ROOT)


if __name__ == "__main__":
    raise SystemExit(main())
