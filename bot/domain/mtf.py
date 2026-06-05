"""Shared multi-timeframe alignment for delivery and strategy audits."""

from __future__ import annotations

from typing import TYPE_CHECKING

from bot.persistence.repository.cache import htf_trend_label, signal_allowed_by_mtf

if TYPE_CHECKING:
    import polars as pl

    from bot.domain.schemas import PreparedSymbol

REVERSAL_PROFILES = frozenset({"countertrend_exhaustion", "divergence_reversal"})
BREAKOUT_PROFILE = "breakout_acceptance"
TREND_FOLLOW_PROFILE = "trend_follow"


def normalize_mtf_reject_reason(reason: str | None) -> str:
    """Map evaluate_mtf_gate reasons to dashboard/telemetry keys."""
    if not reason:
        return "htf_conflict"
    raw = str(reason).strip().lower()
    if raw.startswith("htf_reversal_conflict"):
        return "htf_reversal_conflict"
    if raw.startswith("htf_conflict"):
        return "htf_conflict"
    if raw.startswith("htf_frames_missing"):
        return "htf_frames_missing"
    return raw.split(":", 1)[0]


def mtf_frames(prepared: PreparedSymbol) -> dict[str, pl.DataFrame]:
    frames: dict[str, pl.DataFrame] = {}
    work_1h = getattr(prepared, "work_1h", None)
    work_4h = getattr(prepared, "work_4h", None)
    if work_1h is not None and not work_1h.is_empty():
        frames["1h"] = work_1h
    if work_4h is not None and not work_4h.is_empty():
        frames["4h"] = work_4h
    return frames


def evaluate_mtf_gate(
    prepared: PreparedSymbol,
    direction: str,
    *,
    confirmation_profile: str,
    strict_data_quality: bool = True,
) -> tuple[bool, str, dict[str, object]]:
    """Return whether HTF context allows delivery for this signal profile.

    - trend_follow / breakout_acceptance: strict EMA-trend check on 1h+4h
    - countertrend / divergence_reversal: block only when *both* HTFs oppose
    """
    norm_dir = str(direction or "").strip().lower()
    if norm_dir not in {"long", "short"}:
        return False, "invalid_direction", {}

    frames = mtf_frames(prepared)
    if not frames:
        if strict_data_quality:
            return False, "htf_frames_missing", {"frames": []}
        return True, "mtf_frames_missing", {"frames": []}

    profile = str(confirmation_profile or "trend_follow").strip().lower()
    if profile in REVERSAL_PROFILES:
        conflicts: list[str] = []
        for label, frame in frames.items():
            trend = htf_trend_label(frame)
            if (norm_dir == "long" and trend == "bearish") or (
                norm_dir == "short" and trend == "bullish"
            ):
                conflicts.append(label)
        details: dict[str, object] = {"profile": profile, "conflicts": conflicts}
        if len(conflicts) >= 2:
            return False, "htf_reversal_conflict:" + ",".join(conflicts), details
        return True, "htf_reversal_ok", details

    allowed, reason = signal_allowed_by_mtf(norm_dir, frames)
    return allowed, reason, {"profile": profile, "frames": list(frames)}
