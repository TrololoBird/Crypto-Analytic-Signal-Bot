"""Shadow path after reconcile — log flip candidate when DOM conflicts with path (P2)."""
from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from typing import Any

from hunt_core.deep.verdict_v2.path_mapper import map_to_expected_path
from hunt_core.deep.verdict_v2.types import ExpectedPath, PatternCandidate, PatternConfidence
from hunt_core.paths import RECONCILE_PATH_SHADOW_JSONL

_DOM_FLIP_CONFLICTS = frozenset(
    {
        "dom_buyers_vs_short",
        "dom_sellers_vs_long",
        "dom_buyers_vs_short_strong",
        "dom_sellers_vs_long_strong",
    }
)


def path_shadow_enabled() -> bool:
    return os.getenv("HUNT_RECONCILE_PATH_SHADOW", "1").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def reconcile_flip_path_enabled() -> bool:
    """Production path flip (off by default — shadow log only)."""
    return os.getenv("HUNT_RECONCILE_FLIP_PATH", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def compute_shadow_path(
    row: dict[str, Any],
    path: ExpectedPath,
    *,
    reconcile_conflicts: tuple[str, ...],
    patterns: PatternConfidence,
    topology: Any,
) -> ExpectedPath | None:
    """Opposite-direction path when orderflow DOM conflicts with primary path."""
    if path.direction not in {"long", "short"}:
        return None
    if not any(c in _DOM_FLIP_CONFLICTS for c in reconcile_conflicts):
        return None
    flip_dir = "long" if path.direction == "short" else "short"
    alt_pid = "accumulation" if flip_dir == "long" else "bear_continuation"
    alt = PatternConfidence(
        primary=PatternCandidate(
            id=alt_pid,
            raw_score=max(0.35, patterns.primary.raw_score * 0.85),
            direction_hint=flip_dir,  # type: ignore[arg-type]
            evidence=["reconcile_dom_flip"],
        ),
        alternatives=(patterns.primary,),
        spread=patterns.spread,
        ambiguous=True,
    )
    return map_to_expected_path(row, alt, topology)


def append_reconcile_path_shadow(
    row: dict[str, Any],
    *,
    path: ExpectedPath,
    shadow_path: ExpectedPath | None,
    reconcile_level: str,
    reconcile_conflicts: tuple[str, ...],
    reconcile_caveats: tuple[str, ...],
    action: str,
) -> None:
    if not path_shadow_enabled() or shadow_path is None:
        return
    try:
        from hunt_core.data.jsonl_io import append_jsonl_lines

        record = {
            "ts": row.get("ts") or datetime.now(UTC).isoformat(),
            "symbol": str(row.get("symbol") or "").upper(),
            "reconcile_level": reconcile_level,
            "conflicts": list(reconcile_conflicts),
            "caveats": list(reconcile_caveats),
            "primary_path": path.type,
            "primary_direction": path.direction,
            "shadow_path": shadow_path.type,
            "shadow_direction": shadow_path.direction,
            "action": action,
            "flip_enabled": reconcile_flip_path_enabled(),
        }
        RECONCILE_PATH_SHADOW_JSONL.parent.mkdir(parents=True, exist_ok=True)
        append_jsonl_lines(
            RECONCILE_PATH_SHADOW_JSONL,
            [json.dumps(record, separators=(",", ":"), default=str)],
        )
    except Exception:
        pass


__all__ = [
    "append_reconcile_path_shadow",
    "compute_shadow_path",
    "path_shadow_enabled",
    "reconcile_flip_path_enabled",
]
