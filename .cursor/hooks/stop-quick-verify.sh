#!/usr/bin/env bash
# stop — optional offline smoke verify; fail open on error.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
if [ -x "$ROOT/scripts/agent_quick_verify.sh" ]; then
  "$ROOT/scripts/agent_quick_verify.sh" >/tmp/agent-quick-verify.log 2>&1 || {
    cat <<EOF
{
  "followup_message": "Quick verify failed — read /tmp/agent-quick-verify.log and fix before ending session. Run /verify for full suite."
}
EOF
    exit 0
  }
fi
exit 0
