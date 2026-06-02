"""Unit tests for MetaSignalMerger direction conflict rules (no network)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from bot.domain.schemas import Signal
from bot.runtime.merge import MetaSignalMerger


def _signal(
    *,
    symbol: str = "ETHUSDT",
    setup_id: str = "ema_bounce",
    direction: str = "long",
    score: float = 0.74,
    risk_reward: float = 2.0,
    created_at: datetime | None = None,
) -> Signal:
    if direction == "short":
        entry_low = 3420.0
        entry_high = 3438.0
        entry_mid = (entry_low + entry_high) / 2.0
        stop = 3465.0
        risk = stop - entry_mid
        take_profit_1 = entry_mid - risk * risk_reward
        take_profit_2 = entry_mid - risk * (risk_reward + 0.5)
    else:
        entry_low = 3420.0
        entry_high = 3438.0
        entry_mid = (entry_low + entry_high) / 2.0
        stop = 3395.0
        risk = entry_mid - stop
        take_profit_1 = entry_mid + risk * risk_reward
        take_profit_2 = entry_mid + risk * (risk_reward + 0.5)

    kwargs: dict[str, object] = {
        "symbol": symbol,
        "setup_id": setup_id,
        "direction": direction,
        "score": score,
        "timeframe": "15m",
        "entry_low": entry_low,
        "entry_high": entry_high,
        "stop": stop,
        "take_profit_1": take_profit_1,
        "take_profit_2": take_profit_2,
        "risk_reward": risk_reward,
    }
    if created_at is not None:
        kwargs["created_at"] = created_at
    return Signal(**kwargs)


def test_merge_keeps_single_direction_per_symbol() -> None:
    merger = MetaSignalMerger()
    result = merger.merge(
        [
            _signal(setup_id="fvg_setup", score=0.71),
            _signal(setup_id="order_block", score=0.69),
        ]
    )

    assert len(result.merged) == 1
    assert result.direction_conflicts == []
    assert result.merged[0].primary.setup_id == "fvg_setup"
    assert "confluence_2_setups" in result.merged[0].primary.reasons


def test_same_batch_long_short_higher_score_wins() -> None:
    merger = MetaSignalMerger()
    result = merger.merge(
        [
            _signal(direction="long", score=0.80, setup_id="liquidity_sweep"),
            _signal(direction="short", score=0.76, setup_id="fvg_setup"),
        ]
    )

    assert len(result.merged) == 1
    assert result.merged[0].primary.direction == "long"
    assert len(result.direction_conflicts) == 1
    conflict = result.direction_conflicts[0]
    assert conflict.primary.direction == "short"
    assert "direction_conflict_same_batch" in conflict.primary.reasons
    assert "direction_conflict_winner=long" in conflict.primary.reasons


def test_same_batch_tiebreaker_uses_risk_reward() -> None:
    merger = MetaSignalMerger()
    result = merger.merge(
        [
            _signal(direction="long", score=0.80, risk_reward=1.8),
            _signal(direction="short", score=0.80, risk_reward=2.4, setup_id="fvg_setup"),
        ]
    )

    assert result.merged[0].primary.direction == "short"
    assert result.direction_conflicts[0].primary.direction == "long"


def test_4h_blocks_opposite_direction() -> None:
    now = datetime(2026, 6, 2, 12, 0, tzinfo=UTC)
    recent_long = _signal(
        direction="long",
        setup_id="order_block",
        score=0.78,
        created_at=now - timedelta(hours=2),
    )
    merger = MetaSignalMerger()
    result = merger.merge(
        [_signal(direction="short", score=0.82, setup_id="liquidity_sweep")],
        recent_actions=[recent_long],
        now=now,
    )

    assert result.merged == []
    assert len(result.direction_conflicts) == 1
    conflict = result.direction_conflicts[0]
    assert conflict.primary.direction == "short"
    assert "direction_conflict_4h" in conflict.primary.reasons
    assert "direction_conflict_winner=long" in conflict.primary.reasons


def test_4h_does_not_block_same_direction() -> None:
    now = datetime(2026, 6, 2, 12, 0, tzinfo=UTC)
    recent_long = _signal(
        direction="long",
        setup_id="order_block",
        score=0.78,
        created_at=now - timedelta(hours=2),
    )
    merger = MetaSignalMerger()
    result = merger.merge(
        [_signal(direction="long", score=0.82, setup_id="liquidity_sweep")],
        recent_actions=[recent_long],
        now=now,
    )

    assert len(result.merged) == 1
    assert result.direction_conflicts == []


def test_4h_expired_action_does_not_block() -> None:
    now = datetime(2026, 6, 2, 12, 0, tzinfo=UTC)
    recent_long = _signal(
        direction="long",
        setup_id="order_block",
        score=0.78,
        created_at=now - timedelta(hours=5),
    )
    merger = MetaSignalMerger()
    result = merger.merge(
        [_signal(direction="short", score=0.82, setup_id="liquidity_sweep")],
        recent_actions=[recent_long],
        now=now,
    )

    assert len(result.merged) == 1
    assert result.direction_conflicts == []


def test_same_batch_conflict_before_4h_window() -> None:
    now = datetime(2026, 6, 2, 12, 0, tzinfo=UTC)
    recent_short = _signal(
        direction="short",
        setup_id="order_block",
        score=0.78,
        created_at=now - timedelta(hours=1),
    )
    merger = MetaSignalMerger()
    result = merger.merge(
        [
            _signal(direction="long", score=0.81, setup_id="liquidity_sweep"),
            _signal(direction="short", score=0.79, setup_id="fvg_setup"),
        ],
        recent_actions=[recent_short],
        now=now,
    )

    assert result.merged == []
    assert len(result.direction_conflicts) == 2
    reasons = {meta.primary.direction: meta.primary.reasons for meta in result.direction_conflicts}
    assert "direction_conflict_same_batch" in reasons["short"]
    assert "direction_conflict_4h" in reasons["long"]
