"""Fail-closed config validator — reject removed / unknown hunt sections."""
from __future__ import annotations

import sys
from pathlib import Path

from hunt_core.domain.config import load_config_defaults_toml

REMOVED_SECTIONS = frozenset(
    {
        "gate.premature_exhaustion_short",
    }
)

KNOWN_TOP_SECTIONS = frozenset(
    {
        "bot",
        "hunt",
        "fusion",
        "scanner",
        "watch",
        "pinned",
        "deep",
        "verdict_v2",
        "gate",
        "gates",
        "delivery",
        "maps",
        "expansion",
        "notifiers",
        "network",
        "confirm",
        "levels",
        "scoring",
        "tracker",
        "market_regime",
    }
)


def _flatten_keys(data: dict, prefix: str = "") -> set[str]:
    out: set[str] = set()
    for key, val in data.items():
        path = f"{prefix}.{key}" if prefix else str(key)
        out.add(path)
        if isinstance(val, dict):
            out |= _flatten_keys(val, path)
    return out


def main() -> int:
    defaults = load_config_defaults_toml()
    if not defaults:
        print("check_config: config.defaults.toml missing or empty", file=sys.stderr)
        return 1
    keys = _flatten_keys(defaults)
    issues: list[str] = []
    for removed in REMOVED_SECTIONS:
        if removed in keys or any(k.startswith(f"{removed}.") for k in keys):
            issues.append(f"removed section still present: [{removed}]")
    unknown_roots = {k.split(".", 1)[0] for k in keys} - KNOWN_TOP_SECTIONS
    for root in sorted(unknown_roots):
        issues.append(f"unknown top-level section: [{root}]")
    if issues:
        print(f"check_config: {len(issues)} issue(s)", file=sys.stderr)
        for line in issues:
            print(f"  {line}", file=sys.stderr)
        return 1
    from hunt_core.data.universe import load_pinned_symbols

    pinned = load_pinned_symbols()
    if len(pinned) != 7:
        print(
            f"check_config: pinned set must be 7 symbols, got {len(pinned)} {pinned}",
            file=sys.stderr,
        )
        return 1
    print(f"check_config ok | keys={len(keys)} pinned={len(pinned)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
