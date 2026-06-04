#!/usr/bin/env python3
"""afterFileEdit — run ruff format on edited Python files (best-effort)."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError:
        sys.exit(0)

    file_path = payload.get("file_path") or ""
    tool_input = payload.get("tool_input")
    if not file_path and isinstance(tool_input, dict):
        file_path = tool_input.get("file_path") or tool_input.get("path") or ""
    path = Path(file_path)
    if path.suffix != ".py" or not path.is_file():
        sys.exit(0)

    # Only format project source, not venv or caches
    parts = path.parts
    if ".venv" in parts or "__pycache__" in parts:
        sys.exit(0)

    try:
        subprocess.run(
            ["ruff", "format", str(path)],
            check=False,
            capture_output=True,
            timeout=30,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    sys.exit(0)


if __name__ == "__main__":
    main()
