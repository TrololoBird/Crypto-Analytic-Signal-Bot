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


def attach_verdict_v2_to_row(row: dict[str, Any]) -> dict[str, Any]:
    """Persist JSONL-safe summary; drop non-serializable dataclass on export."""
    v2 = row.get("verdict_v2")
    summary = verdict_v2_to_summary(v2)
    if summary:
        from hunt_core.analysis.deep.verdict_v2.activation import assess_activation

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
