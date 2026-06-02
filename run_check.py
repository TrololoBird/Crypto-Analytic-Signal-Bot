#!/usr/bin/env python
"""Legacy venv smoke check — delegates to scripts/verify_dependencies.py."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parent
_SCRIPT = _REPO / "scripts" / "verify_dependencies.py"


def main() -> int:
    if not _SCRIPT.is_file():
        print(f"Missing {_SCRIPT}")
        return 1
    result = subprocess.run(
        [sys.executable, str(_SCRIPT)],
        cwd=_REPO,
        check=False,
    )
    return int(result.returncode)


if __name__ == "__main__":
    raise SystemExit(main())
