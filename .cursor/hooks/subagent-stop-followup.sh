#!/usr/bin/env bash
# subagentStop — after de-bloat, nudge parent to run verifier.
set -euo pipefail
exec python3 -c '
import json, sys
try:
    p = json.load(sys.stdin)
except json.JSONDecodeError:
    sys.exit(0)
name = (p.get("subagent_name") or p.get("agent_name") or "").lower()
status = (p.get("status") or "").lower()
if "de-bloat" in name or "de_bloat" in name:
    if status in ("completed", "success", "done", ""):
        print(json.dumps({
            "followup_message": "de-bloat finished. Run verifier subagent or /verify; report pass/fail with evidence."
        }))
'
