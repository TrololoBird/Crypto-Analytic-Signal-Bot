"""Disagreement state from conflict matrix."""
from __future__ import annotations

from hunt_core.deep.verdict_v2._helpers import clamp01
from hunt_core.deep.verdict_v2.config import VerdictV2Config
from hunt_core.deep.verdict_v2.types import DisagreementState, HorizonForecast


def classify_disagreement(
    horizons: dict[str, HorizonForecast],
    conflict_matrix: dict[str, float],
    cfg: VerdictV2Config,
) -> DisagreementState:
    vals = list(conflict_matrix.values())
    score = sum(vals) / len(vals) if vals else 0.0
    dominant = max(conflict_matrix, key=conflict_matrix.get) if conflict_matrix else None
    evidence: list[str] = []
    b = horizons.get("B")
    a = horizons.get("A")

    if score < 0.15:
        state = "consensus"
    elif score >= cfg.disagreement_high_threshold:
        state = "divergence"
        evidence.append(f"conflict={score:.2f}")
    elif a and b and a.dominant != b.dominant:
        state = "transition"
        evidence.append(f"a={a.dominant}_b={b.dominant}")
    elif b and b.conviction < 0.1:
        state = "compression"
    elif b and b.conviction > 0.35:
        state = "expansion"
    else:
        state = "exhaustion" if b and b.conviction < 0.18 else "transition"

    return DisagreementState(
        state=state,  # type: ignore[arg-type]
        score=round(clamp01(score), 3),
        conflict_matrix=conflict_matrix,
        dominant_conflict=dominant,
        evidence=evidence,
    )
