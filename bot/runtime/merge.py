"""MetaSignal merge — one canonical candidate per symbol+direction (target spec)."""

from __future__ import annotations

from dataclasses import dataclass, field

from bot.domain.schemas import Signal


@dataclass(slots=True)
class MetaSignal:
    primary: Signal
    aligned_setup_ids: list[str] = field(default_factory=list)
    score_boost: float = 0.0


def merge_candidates(candidates: list[Signal]) -> list[MetaSignal]:
    """Pick best signal per (symbol, direction); attach same-direction confluence."""
    if not candidates:
        return []

    by_key: dict[tuple[str, str], list[Signal]] = {}
    for sig in candidates:
        key = (sig.symbol, str(sig.direction))
        by_key.setdefault(key, []).append(sig)

    merged: list[MetaSignal] = []
    from dataclasses import replace

    for group in by_key.values():
        group.sort(key=lambda s: float(s.score or 0.0), reverse=True)
        primary = group[0]
        aligned = [g.setup_id for g in group[1:] if g.setup_id != primary.setup_id]
        boost = min(0.05, 0.015 * len(aligned))
        reasons = tuple(primary.reasons)
        if aligned:
            reasons = (*reasons, f"confluence_{len(aligned) + 1}_setups")
        primary = replace(
            primary,
            score=float(primary.score or 0.0) + boost,
            reasons=reasons,
        )
        merged.append(MetaSignal(primary=primary, aligned_setup_ids=aligned, score_boost=boost))
    return merged
