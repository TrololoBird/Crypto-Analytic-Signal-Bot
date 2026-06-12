"""Strict schema for offline intel analyst responses (suggestions only)."""

from __future__ import annotations

from typing import Any

REQUIRED_TOP_KEYS = frozenset(
    {"hypotheses", "threshold_suggestions", "strategy_gaps", "risk_flags", "meta"}
)


def empty_report(*, n_signals: int = 0) -> dict[str, Any]:
    return {
        "hypotheses": [],
        "threshold_suggestions": [],
        "strategy_gaps": [],
        "risk_flags": [
            {
                "severity": "info",
                "message": f"Small sample n={n_signals}; refuse strong threshold claims below n≥30.",
            }
        ],
        "meta": {"n_signals": n_signals, "source": "template"},
    }


def validate_intel_report(raw: dict[str, Any], *, min_n: int = 30) -> tuple[bool, list[str]]:
    """Validate analyst JSON; returns (ok, errors)."""
    errors: list[str] = []
    if not isinstance(raw, dict):
        return False, ["root must be object"]
    missing = REQUIRED_TOP_KEYS - set(raw.keys())
    if missing:
        errors.append(f"missing keys: {sorted(missing)}")

    for key in ("hypotheses", "threshold_suggestions", "strategy_gaps", "risk_flags"):
        if key in raw and not isinstance(raw[key], list):
            errors.append(f"{key} must be list")

    n = 0
    meta = raw.get("meta")
    if isinstance(meta, dict):
        try:
            n = int(meta.get("n_signals") or 0)
        except (TypeError, ValueError):
            errors.append("meta.n_signals must be int")

    for i, item in enumerate(raw.get("threshold_suggestions") or []):
        if not isinstance(item, dict):
            errors.append(f"threshold_suggestions[{i}] must be object")
            continue
        for field in ("param", "value", "rationale", "confidence"):
            if field not in item:
                errors.append(f"threshold_suggestions[{i}] missing {field}")
        conf = item.get("confidence")
        if isinstance(conf, (int, float)) and conf > 0.7 and n < min_n:
            errors.append(
                f"threshold_suggestions[{i}] confidence {conf} too high for n={n} (<{min_n})"
            )

    return len(errors) == 0, errors
