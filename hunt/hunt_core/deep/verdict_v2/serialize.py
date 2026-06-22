"""Verdict V2 row serialization for JSONL / cache."""
from __future__ import annotations

from typing import Any


def verdict_v2_to_summary(v2: Any | None) -> dict[str, Any] | None:
    if v2 is None:
        return None
    to_summary = getattr(v2, "to_summary_dict", None)
    if callable(to_summary):
        return to_summary()
    if isinstance(v2, dict):
        return dict(v2)
    return None


def ensure_verdict_v2(row: dict[str, Any]) -> Any | None:
    """Single authority for attaching Verdict V2 to a deep row.

    Builds the ScenarioVerdict via the orchestrator when not already present as a
    live dataclass, stores it on ``row["verdict_v2"]`` and writes the JSONL-safe
    ``verdict_v2_summary``. Returns the ScenarioVerdict object (or None on a row
    that cannot be scored, e.g. missing timeframes).
    """
    from hunt_core.deep.verdict_v2.types import ScenarioVerdict

    v2 = row.get("verdict_v2")
    if not isinstance(v2, ScenarioVerdict):
        from hunt_core.deep.verdict_v2.orchestrator import build_scenario_verdict

        v2 = build_scenario_verdict(row)
        row["verdict_v2"] = v2
    attach_verdict_v2_to_row(row)
    return v2


def attach_verdict_v2_to_row(row: dict[str, Any]) -> dict[str, Any]:
    """Persist JSONL-safe summary; drop non-serializable dataclass on export."""
    v2 = row.get("verdict_v2")
    summary = verdict_v2_to_summary(v2)
    if summary:
        from hunt_core.deep.verdict_v2.activation import assess_activation

        act = assess_activation(row, summary)
        summary = dict(summary)
        summary["activation"] = act.get("state", "idle")
        summary["activation_detail"] = act.get("detail", "")
        row["verdict_v2_summary"] = summary
    return row


def strip_verdict_v2_for_jsonl(row: dict[str, Any]) -> dict[str, Any]:
    """Remove live dataclass before JSON encode."""
    out = dict(row)
    v2 = out.pop("verdict_v2", None)
    if out.get("verdict_v2_summary") is None and v2 is not None:
        summary = verdict_v2_to_summary(v2)
        if summary:
            out["verdict_v2_summary"] = summary
    return out
