#!/usr/bin/env bash
# sessionStart — inject v9 project reminders for Cursor agent sessions.
set -euo pipefail
cat <<'EOF'
{
  "additional_context": "Solo operator: human directs only — you run all commands. Playbook: docs/SOLO_OPERATOR_PLAYBOOK.md. Loop: /plan-task → /implement-plan → /verify. Python 3.14 .venv. Delivery: contract→confluence→deliver. clean_session_data before live. Subagents: orchestrator, live-ops, de-bloat, verifier, delivery-guardian."
}
EOF
