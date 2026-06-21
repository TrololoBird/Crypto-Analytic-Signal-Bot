"""Shared constructors for block results."""
from __future__ import annotations

from hunt_core.analysis.expansion_engine._util import clamp01
from hunt_core.analysis.expansion_engine.types import BlockResult


def result(
    name: str,
    score: float,
    *,
    direction: str = "neutral",
    evidence: tuple[str, ...] = (),
) -> BlockResult:
    return BlockResult(
        name=name,
        score=clamp01(score),
        direction=direction,  # type: ignore[arg-type]
        active=True,
        evidence=evidence,
    )


def abstain(name: str) -> BlockResult:
    return BlockResult(name=name, score=0.0, direction="neutral", active=False, evidence=())


__all__ = ["abstain", "result"]
