#!/usr/bin/env bash
# Verify GITHUB_TOKEN from .env: auth, repo access, workflow push capability hint.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SCRIPT="$ROOT/scripts/gh_with_env_token.sh"

echo "=== GitHub token verify ==="

if [[ ! -x "$SCRIPT" ]]; then
  chmod +x "$SCRIPT"
fi

"$SCRIPT" auth status 2>&1 | head -5 || true

LOGIN="$("$SCRIPT" api user --jq .login 2>/dev/null || true)"
if [[ -z "$LOGIN" ]]; then
  echo "[FAIL] GITHUB_TOKEN invalid or missing API access" >&2
  exit 1
fi
echo "[OK] Authenticated as: $LOGIN"

REPO="${GITHUB_REPOSITORY:-TrololoBird/Crypto-Analytic-Signal-Bot}"
if "$SCRIPT" api "repos/${REPO}" --jq .full_name >/dev/null 2>&1; then
  echo "[OK] Repository access: $REPO"
else
  echo "[FAIL] No access to $REPO — check token repository scope" >&2
  exit 1
fi

# Workflow scope: read default branch workflow (does not mutate).
if "$SCRIPT" api "repos/${REPO}/contents/.github/workflows/ci.yml" --jq .name >/dev/null 2>&1; then
  echo "[OK] Can read workflows (Contents read)"
else
  echo "[WARN] Cannot read .github/workflows/ci.yml — check Contents permission" >&2
fi

echo "[OK] Token ready for: ./scripts/gh_with_env_token.sh …  and  ./scripts/github_push.sh"
echo "Doc: docs/GITHUB_CURSOR_SETUP.md"
