#!/usr/bin/env bash
# Push current branch using GITHUB_TOKEN from .env (HTTPS, no credential helper).
set -euo pipefail

BRANCH="${1:-$(git rev-parse --abbrev-ref HEAD)}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ENV_FILE="${ENV_FILE:-$ROOT/.env}"

# shellcheck source=load_env.sh
source "$ROOT/scripts/load_env.sh"
load_env_file "$ENV_FILE"

if [[ -z "${GITHUB_TOKEN:-}" ]]; then
  echo "[ERROR] GITHUB_TOKEN is not set in $ENV_FILE" >&2
  echo "See docs/GITHUB_CURSOR_SETUP.md" >&2
  exit 1
fi

REMOTE_URL="$(git remote get-url origin)"
if [[ "$REMOTE_URL" =~ github\.com[:/]([^/]+)/([^/.]+)(\.git)?$ ]]; then
  OWNER="${BASH_REMATCH[1]}"
  REPO="${BASH_REMATCH[2]%.git}"
else
  echo "[ERROR] Cannot parse GitHub owner/repo from origin: $REMOTE_URL" >&2
  exit 1
fi

PUSH_URL="https://x-access-token:${GITHUB_TOKEN}@github.com/${OWNER}/${REPO}.git"
echo "[push] ${BRANCH} -> origin (${OWNER}/${REPO})"
git push "$PUSH_URL" "HEAD:${BRANCH}"
