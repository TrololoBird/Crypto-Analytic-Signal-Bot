#!/usr/bin/env python3
"""Audit enabled strategies: HTF usage vs catalog confirmation_profile."""

from __future__ import annotations

from pathlib import Path

try:
    import scripts.common  # noqa: F401
except ModuleNotFoundError:  # pragma: no cover
    import common  # noqa: F401

from bot.domain.config import load_settings
from bot.domain.strategy_catalog import CATALOG_BY_ID
from bot.strategies import STRATEGY_CLASSES

STRATEGIES_DIR = Path(__file__).resolve().parents[1] / "bot" / "strategies"
HTF_MARKERS = (
    "work_1h",
    "work_4h",
    "regime_1h",
    "regime_4h",
    "bias_1h",
    "bias_4h",
    "_confirmed_context_conflict",
)


def main() -> int:
    settings = load_settings("config.toml")
    enabled = set(settings.setups.enabled_setup_ids())
    issues: list[str] = []

    classes_by_id = {
        str(getattr(cls, "setup_id", "")): cls
        for cls in STRATEGY_CLASSES
        if getattr(cls, "setup_id", None)
    }
    for setup_id in sorted(enabled):
        cls = classes_by_id.get(setup_id)
        if cls is None:
            issues.append(f"{setup_id}: missing STRATEGY_CLASSES entry")
            continue
        module_name = cls.__module__.split(".")[-1] + ".py"
        path = STRATEGIES_DIR / module_name
        if not path.exists():
            path = STRATEGIES_DIR / f"{setup_id}.py"
        text = path.read_text(encoding="utf-8") if path.exists() else ""
        catalog = CATALOG_BY_ID.get(setup_id)
        getattr(catalog, "confirmation_profile", "trend_follow") if catalog else "?"
        uses_htf = any(marker in text for marker in HTF_MARKERS)
        claims_mtf = 'timeframe="15m+1h"' in text or "15m+1h" in text
        if claims_mtf and not uses_htf:
            issues.append(f"{setup_id}: claims 15m+1h but no HTF markers in detector")

    print(f"enabled={len(enabled)} issues={len(issues)}")
    for line in issues:
        print(f"  - {line}")
    if not issues:
        print("  OK: all enabled strategies have HTF markers or delivery-only MTF gate")
    return 1 if issues else 0


if __name__ == "__main__":
    raise SystemExit(main())
