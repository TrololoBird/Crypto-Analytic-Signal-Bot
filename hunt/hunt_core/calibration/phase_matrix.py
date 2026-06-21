"""Phase×direction outcome buckets for hunt_calibration.json (post-gate migration)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

DEFAULT_MIN_SAMPLES = 8
DEFAULT_MAX_WR = 0.42
DEFAULT_PRIOR_WR = 0.45


@dataclass(frozen=True)
class PhaseStats:
    phase: str
    direction: str
    wins: int
    losses: int

    @property
    def n(self) -> int:
        return self.wins + self.losses


def _outcome_kind(row: dict[str, Any]) -> str | None:
    pnl = row.get("pnl_pct")
    if pnl is None:
        return None
    try:
        pnl_f = float(pnl)
    except (TypeError, ValueError):
        return None
    if pnl_f > 0:
        return "win"
    if pnl_f < 0:
        return "loss"
    return None


def _labeled_outcomes(
    signals: dict[str, Any],
    *,
    extra_closed: list[dict[str, Any]] | None = None,
) -> list[tuple[str, str, str]]:
    rows: list[dict[str, Any]] = []
    for sig in signals.values():
        if isinstance(sig, dict) and sig.get("status") == "closed":
            rows.append(sig)
    if extra_closed:
        rows.extend(sig for sig in extra_closed if isinstance(sig, dict))
    out: list[tuple[str, str, str]] = []
    for row in rows:
        kind = _outcome_kind(row)
        if kind is None:
            continue
        phase = str(
            row.get("entry_lifecycle_phase")
            or row.get("setup_phase")
            or row.get("phase")
            or "unknown"
        )
        direction = str(row.get("direction") or "?")
        out.append((phase, direction, kind))
    return out


def export_phase_calibration() -> dict[str, Any]:
    """Serialize phase×direction WR buckets for hunt_calibration.json."""
    from hunt_core.track.tracker import load_tracker_state

    state = load_tracker_state()
    signals = state.get("signals") or {}
    closed_history = state.get("closed_history") or []
    buckets: dict[tuple[str, str], list[str]] = {}
    for phase, direction, kind in _labeled_outcomes(
        signals,
        extra_closed=closed_history if isinstance(closed_history, list) else None,
    ):
        buckets.setdefault((phase, direction), []).append(kind)

    min_n = DEFAULT_MIN_SAMPLES
    max_wr = DEFAULT_MAX_WR
    prior_wr = DEFAULT_PRIOR_WR
    pairs: dict[str, dict[str, Any]] = {}
    disabled_keys: list[str] = []
    for (phase, direction), kinds in sorted(buckets.items()):
        wins = sum(1 for k in kinds if k == "win")
        losses = sum(1 for k in kinds if k == "loss")
        stats = PhaseStats(phase=phase, direction=direction, wins=wins, losses=losses)
        n0 = 4.0
        adj_wr = (wins + prior_wr * n0) / (stats.n + n0) if stats.n else prior_wr
        key = f"{phase}:{direction}"
        pairs[key] = {
            "phase": phase,
            "direction": direction,
            "wins": wins,
            "losses": losses,
            "n": stats.n,
            "wr": round(wins / stats.n, 3) if stats.n else None,
            "adj_wr": round(adj_wr, 3),
            "disabled": stats.n >= min_n and adj_wr <= max_wr,
        }
        if pairs[key]["disabled"]:
            disabled_keys.append(key)

    return {
        "pairs": pairs,
        "disabled_keys": disabled_keys,
        "min_samples": min_n,
        "max_wr": max_wr,
        "prior_wr": prior_wr,
    }


__all__ = ["export_phase_calibration"]
