"""Startup repair for legacy pending rows stuck after zone touch."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from bot.persistence.tracking import SignalTracker


def _pending_row(*, zone_at: str | None, activated_at: str | None = None) -> dict:
    now = datetime.now(UTC)
    expires = (now + timedelta(hours=2)).isoformat()
    now_iso = now.isoformat()
    return {
        "tracking_id": "tid1",
        "tracking_ref": "REF1",
        "signal_key": "BTCUSDT:ema_bounce:long",
        "symbol": "BTCUSDT",
        "setup_id": "ema_bounce",
        "direction": "long",
        "timeframe": "15m",
        "created_at": now_iso,
        "pending_expires_at": expires,
        "active_expires_at": expires,
        "entry_low": 100.0,
        "entry_high": 101.0,
        "entry_mid": 100.5,
        "stop": 98.0,
        "take_profit_1": 104.0,
        "take_profit_2": 106.0,
        "take_profit_3": 108.0,
        "score": 0.72,
        "risk_reward": 2.0,
        "status": "pending",
        "entry_zone_touched_at": zone_at,
        "activated_at": activated_at,
        "last_price": 100.6,
        "reasons": [],
    }


@pytest.mark.asyncio
async def test_repair_stuck_pending_promotes_zone_touch_rows() -> None:
    zone_at = "2026-06-01T12:00:00+00:00"
    memory_repo = MagicMock()
    memory_repo.get_active_signals = AsyncMock(return_value=[_pending_row(zone_at=zone_at)])
    memory_repo.save_active_signal = AsyncMock()
    memory_repo.increment_tracking_stats = AsyncMock()
    memory_repo.get_tracking_stats = AsyncMock(return_value={"activated": 0})

    settings = SimpleNamespace(
        tracking=SimpleNamespace(enabled=True, outcome_retention_days=90),
        features_store_file=None,
    )
    telemetry = MagicMock()
    telemetry.append_jsonl = MagicMock()

    tracker = SignalTracker(
        settings,  # type: ignore[arg-type]
        market_data=None,
        telemetry=telemetry,  # type: ignore[arg-type]
        memory_repo=memory_repo,  # type: ignore[arg-type]
    )
    tracker._persist_tracking_state = AsyncMock()  # type: ignore[method-assign]

    events = await tracker.repair_stuck_pending_activations(dry_run=False)

    assert len(events) == 1
    assert events[0].event_type == "activated"
    assert events[0].note == "legacy_zone_touch_repair"
    assert events[0].tracked.status == "active"
    assert events[0].tracked.activated_at is not None
    memory_repo.increment_tracking_stats.assert_any_call(activated=1)
    tracker._persist_tracking_state.assert_awaited_once()


@pytest.mark.asyncio
async def test_repair_skips_pending_without_zone_touch() -> None:
    memory_repo = MagicMock()
    memory_repo.get_active_signals = AsyncMock(return_value=[_pending_row(zone_at=None)])

    settings = SimpleNamespace(
        tracking=SimpleNamespace(enabled=True, outcome_retention_days=90),
        features_store_file=None,
    )
    tracker = SignalTracker(
        settings,  # type: ignore[arg-type]
        market_data=None,
        telemetry=MagicMock(),  # type: ignore[arg-type]
        memory_repo=memory_repo,  # type: ignore[arg-type]
    )

    events = await tracker.repair_stuck_pending_activations(dry_run=False)
    assert events == []
