"""MetaSignal merge - one canonical candidate per symbol+direction (target spec)."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

    from bot.domain.schemas import Signal

DEFAULT_ACTION_WINDOW_HOURS = 4.0
# answers50 Q34: EMA bounce + Keltner = one MA-channel leg, not two confluence votes.
_MA_CHANNEL_SETUPS = frozenset({"ema_bounce", "keltner_breakout"})


@dataclass(slots=True)
class MetaSignal:
    primary: Signal
    aligned_setup_ids: list[str] = field(default_factory=list)
    score_boost: float = 0.0


@dataclass(slots=True)
class MergeResult:
    """Winners per symbol after direction conflict resolution."""

    merged: list[MetaSignal]
    direction_conflicts: list[MetaSignal] = field(default_factory=list)
    # answers50 Q45: bias-resolved losers — hard drop, never WATCH tier.
    direction_dropped: list[MetaSignal] = field(default_factory=list)


class MetaSignalMerger:
    """Collapse candidates to one signal per symbol+direction processing window."""

    def __init__(self, *, action_window_hours: float = DEFAULT_ACTION_WINDOW_HOURS) -> None:
        self._action_window_hours = action_window_hours

    @staticmethod
    def _group_key(signal: Signal) -> tuple[str, str]:
        return signal.symbol, str(signal.direction or "")

    @staticmethod
    def _rank_key(signal: Signal) -> tuple[float, float]:
        return float(signal.score or 0.0), float(signal.risk_reward or 0.0)

    @staticmethod
    def _dedupe_ma_channel_setups(primary_id: str, aligned_ids: list[str]) -> list[str]:
        ma_primary = primary_id in _MA_CHANNEL_SETUPS
        deduped: list[str] = []
        for setup_id in aligned_ids:
            if setup_id in _MA_CHANNEL_SETUPS:
                if ma_primary:
                    continue
                ma_primary = True
            deduped.append(setup_id)
        return deduped

    @staticmethod
    def _is_opposite_direction(left: str, right: str) -> bool:
        return {str(left or "").lower(), str(right or "").lower()} == {"long", "short"}

    @staticmethod
    def _preferred_direction_for_bias_4h(bias_4h: str) -> str | None:
        """answers50 Q45: uptrend→long, downtrend→short, neutral→no preference."""
        bias = str(bias_4h or "neutral").lower()
        if bias == "uptrend":
            return "long"
        if bias == "downtrend":
            return "short"
        return None

    @staticmethod
    def _bias_4h_from_signal(signal: Signal) -> str:
        return str(getattr(signal, "bias_4h", None) or "neutral").lower()

    def _pick_bias_aligned_winner(
        self,
        metas: list[MetaSignal],
    ) -> tuple[MetaSignal | None, list[MetaSignal]]:
        """Resolve long+short on one symbol via 4H bias (Q45b/c)."""
        preferred = self._preferred_direction_for_bias_4h(
            self._bias_4h_from_signal(metas[0].primary)
        )
        reason_tag = "direction_conflict_4h_bias"
        if preferred is None:
            return None, [
                self._mark_direction_conflict(
                    meta,
                    winner=metas[0].primary,
                    reason_tag="direction_conflict_neutral_4h",
                )
                for meta in metas
            ]

        aligned = [meta for meta in metas if str(meta.primary.direction or "").lower() == preferred]
        if not aligned:
            return None, [
                self._mark_direction_conflict(
                    meta,
                    winner=metas[0].primary,
                    reason_tag=reason_tag,
                )
                for meta in metas
            ]

        ranked = sorted(aligned, key=lambda meta: self._rank_key(meta.primary), reverse=True)
        winner = ranked[0]
        dropped = [
            self._mark_direction_conflict(
                meta,
                winner=winner.primary,
                reason_tag=reason_tag,
            )
            for meta in metas
            if meta is not winner
        ]
        return winner, dropped

    def _within_action_window(self, signal: Signal, *, now: datetime) -> bool:
        created_at = signal.created_at.astimezone(UTC)
        return bool(now - created_at <= timedelta(hours=self._action_window_hours))

    def _merge_per_direction(self, candidates: list[Signal]) -> list[MetaSignal]:
        by_key: dict[tuple[str, str], list[Signal]] = {}
        for signal in candidates:
            by_key.setdefault(self._group_key(signal), []).append(signal)

        merged: list[MetaSignal] = []
        for group in by_key.values():
            ranked = sorted(group, key=self._rank_key, reverse=True)
            primary = ranked[0]
            aligned_setup_ids = MetaSignalMerger._dedupe_ma_channel_setups(
                primary.setup_id,
                sorted(
                    {
                        candidate.setup_id
                        for candidate in ranked[1:]
                        if candidate.setup_id and candidate.setup_id != primary.setup_id
                    }
                ),
            )
            boost = min(0.05, 0.015 * len(aligned_setup_ids))
            reasons = list(primary.reasons)
            if aligned_setup_ids:
                count_reason = f"confluence_{len(aligned_setup_ids) + 1}_setups"
                setups_reason = "confluence_setups=" + ",".join(
                    [primary.setup_id, *aligned_setup_ids]
                )
                if count_reason not in reasons:
                    reasons.append(count_reason)
                if setups_reason not in reasons:
                    reasons.append(setups_reason)
            merged.append(
                MetaSignal(
                    primary=replace(
                        primary,
                        score=min(1.0, float(primary.score or 0.0) + boost),
                        reasons=tuple(reasons),
                    ),
                    aligned_setup_ids=aligned_setup_ids,
                    score_boost=boost,
                )
            )
        return merged

    @staticmethod
    def _mark_direction_conflict(
        meta: MetaSignal,
        *,
        winner: Signal,
        reason_tag: str,
    ) -> MetaSignal:
        reasons = list(meta.primary.reasons)
        for tag in (
            reason_tag,
            "direction_conflict",
            f"direction_conflict_winner={winner.direction}",
        ):
            if tag not in reasons:
                reasons.append(tag)
        return MetaSignal(
            primary=replace(meta.primary, reasons=tuple(reasons)),
            aligned_setup_ids=meta.aligned_setup_ids,
            score_boost=meta.score_boost,
        )

    def _resolve_same_batch_conflicts(
        self,
        per_direction: list[MetaSignal],
    ) -> tuple[list[MetaSignal], list[MetaSignal]]:
        by_symbol: dict[str, list[MetaSignal]] = {}
        for meta in per_direction:
            by_symbol.setdefault(meta.primary.symbol, []).append(meta)

        winners: list[MetaSignal] = []
        dropped: list[MetaSignal] = []
        for metas in by_symbol.values():
            if len(metas) == 1:
                winners.append(metas[0])
                continue
            winner, losers = self._pick_bias_aligned_winner(metas)
            if winner is not None:
                winners.append(winner)
            dropped.extend(losers)
        return winners, dropped

    def _apply_recent_action_conflicts(
        self,
        winners: list[MetaSignal],
        *,
        recent_actions: Sequence[Signal],
        now: datetime,
    ) -> tuple[list[MetaSignal], list[MetaSignal]]:
        recent_by_symbol: dict[str, list[Signal]] = {}
        for action in recent_actions:
            if self._within_action_window(action, now=now):
                recent_by_symbol.setdefault(action.symbol, []).append(action)

        kept: list[MetaSignal] = []
        dropped: list[MetaSignal] = []
        for meta in winners:
            blockers = [
                action
                for action in recent_by_symbol.get(meta.primary.symbol, ())
                if self._is_opposite_direction(action.direction, meta.primary.direction)
            ]
            if not blockers:
                kept.append(meta)
                continue
            blocker = max(blockers, key=lambda action: action.created_at)
            preferred = self._preferred_direction_for_bias_4h(
                self._bias_4h_from_signal(meta.primary)
            )
            meta_dir = str(meta.primary.direction or "").lower()
            if preferred == meta_dir:
                # Q45b: 4H-aligned candidate overrides recent opposite ACTION.
                kept.append(meta)
                continue
            dropped.append(
                self._mark_direction_conflict(
                    meta,
                    winner=blocker,
                    reason_tag="direction_conflict_recent_action",
                )
            )
        return kept, dropped

    def merge(
        self,
        candidates: list[Signal],
        *,
        recent_actions: Sequence[Signal] | None = None,
        now: datetime | None = None,
    ) -> MergeResult:
        """Pick best signal per symbol+direction and resolve direction conflicts."""
        if not candidates:
            return MergeResult(merged=[], direction_conflicts=[], direction_dropped=[])

        resolved_now = now.astimezone(UTC) if now is not None else datetime.now(UTC)
        per_direction = self._merge_per_direction(candidates)
        winners, same_batch_dropped = self._resolve_same_batch_conflicts(per_direction)
        kept, window_dropped = self._apply_recent_action_conflicts(
            winners,
            recent_actions=recent_actions or (),
            now=resolved_now,
        )
        return MergeResult(
            merged=kept,
            direction_dropped=[*same_batch_dropped, *window_dropped],
        )


def merge_candidates(
    candidates: list[Signal],
    *,
    recent_actions: Sequence[Signal] | None = None,
    now: datetime | None = None,
) -> list[MetaSignal]:
    """Backward-compatible helper for runtime callers."""
    return (
        MetaSignalMerger()
        .merge(
            candidates,
            recent_actions=recent_actions,
            now=now,
        )
        .merged
    )
