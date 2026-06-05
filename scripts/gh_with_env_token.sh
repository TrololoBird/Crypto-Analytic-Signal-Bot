#!/usr/bin/env bash
# Run gh with GITHUB_TOKEN from project .env (never print the token).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ENV_FILE="${ENV_FILE:-$ROOT/.env}"

# shellcheck source=load_env.sh
source "$ROOT/scripts/load_env.sh"
load_env_file "$ENV_FILE"

if [[ -z "${GITHUB_TOKEN:-}" ]]; then
  echo "[ERROR] GITHUB_TOKEN is not set." >&2
  echo "Add GITHUB_TOKEN=... to $ENV_FILE (see docs/GITHUB_CURSOR_SETUP.md)" >&2
  exit 1
fi

export GH_TOKEN="$GITHUB_TOKEN"
export GITHUB_TOKEN
exec gh "$@"
