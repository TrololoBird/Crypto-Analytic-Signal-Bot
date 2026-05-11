#!/bin/bash
# List and review completed Jules swarm sessions.
#
# Usage:
#   ./scripts/review.sh           # List all sessions
#   ./scripts/review.sh <id>      # Pull diff for a specific session
#   ./scripts/review.sh <id> apply  # Apply session changes locally

set -euo pipefail

if [ $# -eq 0 ]; then
  echo "Recent Jules sessions:"
  echo "---"
  jules remote list --session
  echo "---"
  echo "To review a session: ./scripts/review.sh <session-id>"
  echo "To apply a session:  ./scripts/review.sh <session-id> apply"
  exit 0
fi

SESSION_ID="$1"
ACTION="${2:-review}"

if [ "$ACTION" = "apply" ]; then
  echo "Applying session $SESSION_ID locally..."
  jules teleport "$SESSION_ID"
else
  echo "Pulling diff for session $SESSION_ID..."
  jules remote pull --session "$SESSION_ID"
fi
