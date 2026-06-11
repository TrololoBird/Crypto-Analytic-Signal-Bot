"""Auto-disable phase×direction combos with poor tracker WR (n≥10, WR under 25%)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from hunt_watch.param_store import phase_matrix_thresholds
from hunt_watch.paths import SIGNAL_STATE
from hunt_watch.signal_tracker import load_tracker_state
from hunt_watch.tracker_outcomes import LOSS_REASONS, WIN_REASONS, entry_lifecycle_phase, outcome_kind

DEFAULT_MIN_SAMPLES = 12
DEFAULT_MAX_WR = 0.28
DEFAULT_PRIOR_WR = 0.35

_CACHE_MTIME: float = -1.0
_CACHE_DISABLED: dict[tuple[str, str], PhaseStats] = {}


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


def _labeled_outcomes(signals: dict[str, Any]) -> list[tuple[str, str, str]]:
    rows: list[tuple[str, str, str]] = []
    for sig in signals.values():
        if not isinstance(sig, dict) or sig.get("status") != "closed":
            continue
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
    state = load_tracker_state()
    signals = state.get("signals") or {}
    buckets: dict[tuple[str, str], list[str]] = {}
    for phase, direction, kind in _labeled_outcomes(signals):
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
        # Bayesian shrinkage: (w + prior*n0) / (n + n0) with n0≈4 pseudo-samples
        n0 = 4.0
        adj_wr = (wins + prior_wr * n0) / (stats.n + n0) if stats.n else prior_wr
        if stats.n >= min_n and adj_wr < max_wr:
            disabled[(phase, direction)] = stats
    return disabled


def disabled_phase_pairs(*, force: bool = False) -> dict[tuple[str, str], PhaseStats]:
    global _CACHE_MTIME, _CACHE_DISABLED
    mtime = SIGNAL_STATE.stat().st_mtime if SIGNAL_STATE.is_file() else 0.0
    if force or mtime != _CACHE_MTIME:
        _CACHE_DISABLED = _rebuild_cache()
        _CACHE_MTIME = mtime
    return dict(_CACHE_DISABLED)


def phase_matrix_gate(phase: str, direction: str) -> tuple[bool, str]:
    """Return (blocked, human reason). Empty phase → not blocked."""
    if not phase or phase == "no_setup":
        return False, ""
    key = (phase, str(direction))
    stats = disabled_phase_pairs().get(key)
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
