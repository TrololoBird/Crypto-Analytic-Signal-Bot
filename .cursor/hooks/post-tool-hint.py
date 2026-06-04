#!/usr/bin/env python3
"""postToolUse — verification hint after Shell/Write on bot/ (Cursor)."""
from __future__ import annotations

import json
import sys


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError:
        sys.exit(0)

    tool_name = payload.get("tool_name") or ""
    tool_input = payload.get("tool_input") or {}
    path = ""
    if isinstance(tool_input, dict):
        path = tool_input.get("file_path") or tool_input.get("path") or ""

    hints: list[str] = []
    if tool_name in {"Write", "Edit", "search_replace", "ApplyPatch"} and "/bot/" in path.replace("\\", "/"):
        hints.append("Run: python -m compileall -q bot && make graphify-update")
    if tool_name == "Shell":
        cmd = ""
        if isinstance(tool_input, dict):
            cmd = tool_input.get("command") or tool_input.get("cmd") or ""
        if "main.py run" in cmd or "live_smoke" in cmd or "live_supervised" in cmd:
            hints.append("Ensure clean_session_data.py --mode smoke ran before this live session.")

    if hints:
        print(json.dumps({"additional_context": " ".join(hints)}))

    sys.exit(0)


if __name__ == "__main__":
    main()
