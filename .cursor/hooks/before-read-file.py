#!/usr/bin/env python3
"""beforeReadFile — deny reads of local secrets and large runtime artifacts."""
from __future__ import annotations

import json
import os
import sys

DENY_SUFFIXES = (
    "/.env",
    "/config.toml",
    "/config.local.toml",
    "/ourtg.json",
)
DENY_PREFIXES = (
    "/data/",
    "/telemetry/",
    "/logs/",
)
DENY_BASENAMES = (
    "bot.db",
)


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError:
        print(json.dumps({"permission": "allow"}))
        return

    file_path = payload.get("file_path") or ""
    normalized = file_path.replace("\\", "/")
    basename = os.path.basename(normalized)

    deny_reason = None
    if basename in DENY_BASENAMES:
        deny_reason = f"runtime artifact: {basename}"
    else:
        for suffix in DENY_SUFFIXES:
            if normalized.endswith(suffix) or f"{suffix}/" in normalized:
                deny_reason = f"sensitive local config: {basename}"
                break
        if not deny_reason:
            for prefix in DENY_PREFIXES:
                if f"{prefix}" in normalized or normalized.endswith(prefix.rstrip("/")):
                    deny_reason = f"runtime data path: {prefix}"
                    break

    if deny_reason:
        print(
            json.dumps(
                {
                    "permission": "deny",
                    "user_message": (
                        f"Hook blocked read of {basename} ({deny_reason}). "
                        "Use config.toml.example / env.example or ask agent to summarize logs."
                    ),
                }
            )
        )
        return

    print(json.dumps({"permission": "allow"}))


if __name__ == "__main__":
    main()
