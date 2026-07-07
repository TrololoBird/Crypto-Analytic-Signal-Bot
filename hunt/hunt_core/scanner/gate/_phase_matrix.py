"""Phase×direction outcome matrix — auto-disable weak phase pairs (C2)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from hunt_core.params.store import phase_matrix_thresholds
from hunt_core.paths import SIGNAL_STATE
from hunt_core.track.outcomes import entry_lifecycle_phase, outcome_kind

# Module-local cache for callers that don't pass SymbolStateStore
# (avoids importing runtime.state).
_local_phase_mtime: float = -1.0
_local_phase_disabled: dict[tuple[str, str], Any] = {}

# NOTE: hunt_core.track.tracker is imported lazily inside the functions below.
# It pulls scan -> gate, so a top-level import here would create a circular
# import whenever _phase_matrix loads before track.tracker finishes (now that
# phase_matrix_gate is wired into the live gate stack).

DEFAULT_MIN_SAMPLES = 12
DEFAULT_MAX_WR = 0.28
DEFAULT_PRIOR_WR = 0.35


@dataclass(frozen=True, slots=True)
class PhaseStats:
    phase: str
    direction: str
    wins: int
    losses: int

    @property
    def n(self) -> int:
        return self.wins + self.losses

    @property
    def wr(self) -> float:
        return self.wins / self.n if self.n else 0.0


def _labeled_outcomes(
    signals: dict[str, Any],
    *,
    extra_closed: list[dict[str, Any]] | None = None,
) -> list[tuple[str, str, str]]:
    rows: list[tuple[str, str, str]] = []
    seen_message_ids: set[int] = set()
    sources: list[dict[str, Any]] = [
        sig
        for sig in signals.values()
        if isinstance(sig, dict) and sig.get("status") == "closed"
    ]
    if extra_closed:
        sources.extend(sig for sig in extra_closed if isinstance(sig, dict))
    for sig in sources:
        eid = sig.get("entry_message_id")
        if eid is not None:
            try:
                eid_int = int(eid)
            except (TypeError, ValueError):
                eid_int = None
            if eid_int is not None:
                if eid_int in seen_message_ids:
                    continue
                seen_message_ids.add(eid_int)
        reason = str(sig.get("close_reason") or "unknown")
        pnl = sig.get("pnl_pct")
        if pnl is None:
            continue
        phase = entry_lifecycle_phase(sig)
        direction = str(sig.get("direction") or "")
        if not phase or phase == "?" or direction not in {"long", "short"}:
            continue
        kind = outcome_kind(reason, pnl_pct=float(pnl))
        if kind not in {"win", "loss"}:
            continue
        rows.append((phase, direction, "win" if kind == "win" else "loss"))
    return rows


def _rebuild_cache() -> dict[tuple[str, str], PhaseStats]:
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

    disabled: dict[tuple[str, str], PhaseStats] = {}
    pm = phase_matrix_thresholds()
    min_n = int(pm.get("min_samples", DEFAULT_MIN_SAMPLES))
    max_wr = float(pm.get("max_wr", DEFAULT_MAX_WR))
    prior_wr = float(pm.get("prior_wr", DEFAULT_PRIOR_WR))
    for (phase, direction), kinds in buckets.items():
        wins = sum(1 for k in kinds if k == "win")
        losses = sum(1 for k in kinds if k == "loss")
        stats = PhaseStats(phase=phase, direction=direction, wins=wins, losses=losses)
        n0 = 4.0
        adj_wr = (wins + prior_wr * n0) / (stats.n + n0) if stats.n else prior_wr
        if stats.n >= min_n and adj_wr < max_wr:
            disabled[(phase, direction)] = stats
    return disabled


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

    pm = phase_matrix_thresholds()
    min_n = int(pm.get("min_samples", DEFAULT_MIN_SAMPLES))
    max_wr = float(pm.get("max_wr", DEFAULT_MAX_WR))
    prior_wr = float(pm.get("prior_wr", DEFAULT_PRIOR_WR))
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
            "wr": round(stats.wr, 3),
            "adj_wr": round(adj_wr, 3),
        }
        if stats.n >= min_n and adj_wr < max_wr:
            disabled_keys.append(key)
    return {
        "min_samples": min_n,
        "max_wr": max_wr,
        "prior_wr": prior_wr,
        "pairs": pairs,
        "disabled": disabled_keys,
    }


def disabled_phase_pairs(
    *,
    force: bool = False,
    state: Any | None = None,
) -> dict[tuple[str, str], PhaseStats]:
    if state is not None:
        store = state
        mtime = SIGNAL_STATE.stat().st_mtime if SIGNAL_STATE.is_file() else 0.0
        if force or mtime != store.phase_matrix_mtime:
            store.phase_matrix_disabled = _rebuild_cache()
            store.phase_matrix_mtime = mtime
        return dict(store.phase_matrix_disabled)
    global _local_phase_mtime
    mtime = SIGNAL_STATE.stat().st_mtime if SIGNAL_STATE.is_file() else 0.0
    if force or mtime != _local_phase_mtime:
        _local_phase_disabled.clear()
        _local_phase_disabled.update(_rebuild_cache())
        _local_phase_mtime = mtime
    return dict(_local_phase_disabled)


def phase_matrix_gate(
    phase: str,
    direction: str,
    *,
    state: Any | None = None,
) -> tuple[bool, str]:
    """Return (blocked, human reason). Empty phase → not blocked."""
    from hunt_core.params.store import self_tuning_frozen

    if self_tuning_frozen():
        return False, ""
    if not phase or phase == "no_setup":
        return False, ""
    key = (phase, str(direction))
    stats = disabled_phase_pairs(state=state).get(key)
    if stats is None:
        return False, ""
    pm = phase_matrix_thresholds()
    min_n = int(pm.get("min_samples", DEFAULT_MIN_SAMPLES))
    max_wr = float(pm.get("max_wr", DEFAULT_MAX_WR))
    return (
        True,
        f"Phase {phase} {direction}: WR {stats.wr * 100:.0f}% на n={stats.n} "
        f"(порог {max_wr * 100:.0f}%, min n={min_n}) — auto-off",
    )


__all__ = [
    "DEFAULT_MAX_WR",
    "DEFAULT_MIN_SAMPLES",
    "DEFAULT_PRIOR_WR",
    "PhaseStats",
    "disabled_phase_pairs",
    "export_phase_calibration",
    "phase_matrix_gate",
]
