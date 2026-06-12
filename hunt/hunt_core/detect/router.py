"""H-B setup router — phase × direction → detector path."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

DetectorPath = Literal["short_dump", "long_bounce", "early_advisory", "none"]


@dataclass(frozen=True, slots=True)
class SetupCandidate:
    path: DetectorPath
    direction: str
    setup: dict[str, Any]
    row: dict[str, Any]
    lifecycle: dict[str, Any]


def route_tick(row: dict[str, Any]) -> list[SetupCandidate]:
    """Return candidate setups for this tick (H-B: all active paths)."""
    lifecycle = row.get("lifecycle") or {}
    if not isinstance(lifecycle, dict):
        lifecycle = {}
    out: list[SetupCandidate] = []
    dump = row.get("dump") or {}
    long_b = row.get("long") or {}
    if isinstance(dump, dict) and (dump.get("confirmed") or dump.get("score")):
        out.append(
            SetupCandidate(
                path="short_dump",
                direction="short",
                setup=dump,
                row=row,
                lifecycle=lifecycle,
            )
        )
    if isinstance(long_b, dict) and (long_b.get("confirmed") or long_b.get("score")):
        out.append(
            SetupCandidate(
                path="long_bounce",
                direction="long",
                setup=long_b,
                row=row,
                lifecycle=lifecycle,
            )
        )
    phase = str(lifecycle.get("phase") or "")
    if phase in {"impulse_initiating", "post_dump_bounce", "distribution"}:
        out.append(
            SetupCandidate(
                path="early_advisory",
                direction=str(lifecycle.get("recommended_bias") or "short"),
                setup={"phase": phase, "advisory": True},
                row=row,
                lifecycle=lifecycle,
            )
        )
    return out
