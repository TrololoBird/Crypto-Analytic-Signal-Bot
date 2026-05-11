#!/bin/bash
# Dispatch all Jules swarm agents against a GitHub repository.
#
# Usage:
#   ./scripts/dispatch.sh <owner/repo>
#   ./scripts/dispatch.sh <owner/repo> [agent1 agent2 ...]
#
# Examples:
#   ./scripts/dispatch.sh myorg/my-app                    # All 8 agents
#   ./scripts/dispatch.sh myorg/my-app sentinel verifier  # Only 2 agents

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROMPTS_DIR="$SCRIPT_DIR/../assets/prompts"

REPO="${1:?Usage: dispatch.sh <owner/repo> [agent1 agent2 ...]}"
shift

ALL_AGENTS=(architect bolt navigator palette sentinel verifier janitor scribe)
AGENTS=("${@:-${ALL_AGENTS[@]}}")

# If no specific agents given, use all
if [ ${#AGENTS[@]} -eq 0 ]; then
  AGENTS=("${ALL_AGENTS[@]}")
fi

echo "Dispatching ${#AGENTS[@]} agents to $REPO"
echo "---"

for agent in "${AGENTS[@]}"; do
  PROMPT_FILE="$PROMPTS_DIR/$agent.md"

  if [ ! -f "$PROMPT_FILE" ]; then
    echo "SKIP: No prompt file for '$agent' at $PROMPT_FILE"
    continue
  fi

  echo "Dispatching: $agent"
  jules new --repo "$REPO" "$(cat "$PROMPT_FILE")"
done

echo "---"
echo "Done. Run 'jules remote list --session' to monitor progress."
