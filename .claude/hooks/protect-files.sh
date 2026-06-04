#!/usr/bin/env bash
# Claude PreToolUse — block writes to secrets and local config.
exec python3 -c '
import json, os, sys
raw = sys.stdin.read()
try:
    p = json.loads(raw)
except json.JSONDecodeError:
    sys.exit(0)
ti = p.get("tool_input") or {}
path = ""
if isinstance(ti, dict):
    path = (ti.get("file_path") or ti.get("path") or "").replace("\\", "/")
base = os.path.basename(path)
if base in {".env", "config.toml", "config.local.toml", "ourtg.json"}:
    print(json.dumps({
        "hookSpecificOutput": {
            "permissionDecision": "deny",
            "permissionDecisionReason": "Writes to local secrets/config are blocked; use *.example templates",
        }
    }))
sys.exit(0)
'
