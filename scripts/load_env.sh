#!/usr/bin/env bash
# Safe dotenv loader — skips comments; does not break on special chars in other lines.
set -euo pipefail

load_env_file() {
  local file="${1:?env file path required}"
  [[ -f "$file" ]] || return 0

  while IFS= read -r line || [[ -n "$line" ]]; do
    line="${line%%$'\r'}"
    [[ -z "${line//[[:space:]]/}" ]] && continue
    [[ "$line" =~ ^[[:space:]]*# ]] && continue
    [[ "$line" != *"="* ]] && continue

    local key="${line%%=*}"
    local value="${line#*=}"
    key="${key#"${key%%[![:space:]]*}"}"
    key="${key%"${key##*[![:space:]]}"}"

    # Do not override variables already exported in the shell.
    if [[ -n "$key" && -z "${!key:-}" ]]; then
      export "$key=$value"
    fi
  done < "$file"
}
