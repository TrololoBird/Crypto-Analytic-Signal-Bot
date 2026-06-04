#!/usr/bin/env python3
"""Shell guard for Cursor beforeShellExecution and Claude PreToolUse (Bash)."""
from __future__ import annotations

import json
import os
import re
import sys

DENY_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"git\s+push\b.*(--force|-f)\b", re.I), "force push blocked"),
    (re.compile(r"git\s+push\b.*force-with-lease", re.I), "force push blocked"),
    (re.compile(r"\brm\s+-rf\b", re.I), "recursive force delete blocked"),
    (re.compile(r"\brm\s+-r\s+data/", re.I), "deleting data/ blocked"),
    (re.compile(r"git\s+add\b.*\.env\b", re.I), "do not stage .env"),
    (re.compile(r"git\s+add\b.*config\.toml\b", re.I), "do not stage config.toml"),
]


def _extract_command(payload: dict) -> str:
    command = payload.get("command") or ""
    if command:
        return str(command)

    tool_input = payload.get("tool_input")
    if isinstance(tool_input, dict):
        for key in ("command", "cmd", "script"):
            if tool_input.get(key):
                return str(tool_input[key])

    tool_name = payload.get("tool_name") or ""
    if tool_name == "Bash" and isinstance(tool_input, str):
        return tool_input

    return os.environ.get("CLAUDE_TOOL_INPUT", "")


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError:
        print(json.dumps({"permission": "allow"}))
        return

    command = _extract_command(payload)
    for pattern, reason in DENY_PATTERNS:
        if pattern.search(command):
            print(
                json.dumps(
                    {
                        "permission": "deny",
                        "user_message": f"Hook blocked: {reason}",
                        "agent_message": (
                            f"Shell command blocked by project hook: {reason}. "
                            f"Command: {command[:200]}"
                        ),
                    }
                )
            )
            sys.exit(0)

    print(json.dumps({"permission": "allow"}))


if __name__ == "__main__":
    main()
