#!/usr/bin/env bash
set -euo pipefail

BASE_SHA="${BASE_SHA:-}"
HEAD_SHA="${HEAD_SHA:-}"

if [[ -z "$BASE_SHA" || -z "$HEAD_SHA" ]]; then
  echo "BASE_SHA/HEAD_SHA not provided; skipping doc-change gate."
  exit 0
fi

changed_files="$(git diff --name-only "$BASE_SHA" "$HEAD_SHA")"

if [[ -z "$changed_files" ]]; then
  echo "No changed files detected."
  exit 0
fi

if ! printf '%s\n' "$changed_files" | rg -q '^(bot/application/|bot/websocket/|bot/features($|_|/)|bot/ml($|_|/))'; then
  echo "No architecture-contract paths changed; doc-change gate not required."
  exit 0
fi

if printf '%s\n' "$changed_files" | rg -q '^docs/'; then
  echo "Architecture-contract paths changed and docs/ updates are present."
  exit 0
fi

echo "Architecture-contract paths changed but docs/ was not updated."
echo "Please add/update documentation in docs/ for this PR."
exit 1
