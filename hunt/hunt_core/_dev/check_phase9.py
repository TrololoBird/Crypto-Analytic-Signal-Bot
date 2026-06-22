"""Phase 9 completion gate — prescan, liq docs (no LOC/file-count splits)."""
from __future__ import annotations

import sys
from pathlib import Path

CORE = Path(__file__).resolve().parents[1]

# Semantic splits retained by operator decision (2026-06-22): clear domain boundaries only.
SEMANTIC_SPLITS = (
    "track/tracker.py + track/_tracker_fsm.py",
    "scanner/gate/policy.py + scanner/gate/_policy_*.py",
)


def main() -> int:
    issues: list[str] = []

    defaults = CORE.parent / "config.defaults.toml"
    if defaults.exists():
        text = defaults.read_text(encoding="utf-8")
        if "debounce_s" not in text:
            issues.append("watch.prescan debounce_s missing from config.defaults.toml")
        else:
            for line in text.splitlines():
                if line.strip().startswith("debounce_s"):
                    try:
                        val = float(line.split("=", 1)[1].strip())
                        if not 60 <= val <= 120:
                            issues.append(f"prescan debounce_s={val} outside 60–120s plan range")
                    except ValueError:
                        pass
                    break

    liq_doc = (CORE / "maps/liquidation.py").read_text(encoding="utf-8")
    if "leverage_tier_estimate" not in liq_doc[:800]:
        issues.append("maps/liquidation.py missing leverage_tier_estimate provenance doc")

    for rel in ("track/_tracker_fsm.py", "scanner/gate/_policy_edge.py"):
        if not (CORE / rel).is_file():
            issues.append(f"missing semantic split module: {rel}")

    if issues:
        print(f"check_phase9: {len(issues)} issue(s)", file=sys.stderr)
        for line in issues:
            print(f"  {line}", file=sys.stderr)
        return 1
    print(f"check_phase9 ok | semantic_splits={len(SEMANTIC_SPLITS)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
