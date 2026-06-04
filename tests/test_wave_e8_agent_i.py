"""Wave E8 agent I: migration v5, startup tracking, expired pending notify, audit rows."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from bot.delivery.deliver import SignalDelivery
from bot.migrations import fetch_schema_version, migrate_db
from bot.persistence.db_status import (
    DELIVERY_AUDIT_ROW_KEYS,
    normalize_delivery_audit_row,
)
from bot.persistence.repository.memory import MemoryRepository
from bot.persistence.tracking import SignalTracker
from bot.runtime.startup_digest import format_startup_tracking_digest


def _pending_row(
    *,
    zone_at: str | None = None,
    pending_expires_at: str | None = None,
    signal_message_id: int | None = None,
) -> dict:
    now = datetime.now(UTC)
    expires = pending_expires_at or (now - timedelta(minutes=5)).isoformat()
    return {
        "tracking_id": "tid-expired",
        "tracking_ref": "REF-EXP",
        "signal_key": "BTCUSDT:ema_bounce:long",
        "symbol": "BTCUSDT",
        "setup_id": "ema_bounce",
        "direction": "long",
        "timeframe": "15m",
        "created_at": (now - timedelta(hours=2)).isoformat(),
        "pending_expires_at": expires,
        "active_expires_at": (now + timedelta(hours=1)).isoformat(),
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
        "activated_at": None,
        "last_price": 100.2,
        "signal_message_id": signal_message_id,
        "reasons": [],
    }


@pytest.mark.asyncio
async def test_migration_v5_adds_status_symbol_index(tmp_path) -> None:
    repo = MemoryRepository(tmp_path / "bot.db", tmp_path / "data")
    await repo.initialize()
    conn = repo._require_conn()
    try:
        version_before = await fetch_schema_version(conn)
        applied = await migrate_db(conn)
        version_after = await fetch_schema_version(conn)
        async with conn.execute(
            """
            SELECT name FROM sqlite_master
            WHERE type='index' AND name='idx_active_signals_status_symbol'
            """
        ) as cursor:
            index_row = await cursor.fetchone()
    finally:
        await repo.close()

    assert index_row is not None
    assert version_after >= 5
    assert applied >= 0 or version_before >= 5


def test_startup_digest_includes_repaired_from_summary() -> None:
    text = format_startup_tracking_digest(
        {
            "pending": 2,
            "active": 10,
            "repaired": 4,
            "review_closed": 0,
            "stale_expired": 0,
        }
    )
    assert "Repaired zone-touch" in text
    assert "<code>4</code>" in text


def test_repair_deliverable_filters_message_id_only() -> None:
    with_message = SimpleNamespace(tracked=SimpleNamespace(signal_message_id=42))
    without_message = SimpleNamespace(tracked=SimpleNamespace(signal_message_id=None))
    repair_events = [with_message, without_message]
    deliverable = [
        event
        for event in repair_events
        if getattr(event.tracked, "signal_message_id", None)
    ]
    assert deliverable == [with_message]


@pytest.mark.asyncio
async def test_repair_skips_expired_pending_zone_touch() -> None:
    memory_repo = MagicMock()
    memory_repo.get_active_signals = AsyncMock(
        return_value=[_pending_row(zone_at="2026-06-01T12:00:00+00:00")]
    )

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
    memory_repo.save_active_signal.assert_not_called()


@pytest.mark.asyncio
async def test_review_open_signals_expires_pending_for_channel_follow_up() -> None:
    memory_repo = MagicMock()
    memory_repo.get_active_signals = AsyncMock(
        return_value=[_pending_row(signal_message_id=999)]
    )
    memory_repo.save_active_signal = AsyncMock()
    memory_repo.increment_tracking_stats = AsyncMock()
    memory_repo.get_tracking_stats = AsyncMock(return_value={"expired": 0})

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

    events = await tracker.review_open_signals(dry_run=False)

    assert len(events) == 1
    assert events[0].event_type == "expired"
    assert events[0].note == "pending_expired_channel_notify"
    assert events[0].tracked.signal_message_id == 999
    delivery = SignalDelivery(SimpleNamespace(), pending_expiry_minutes=180)
    assert delivery._should_send_tracking_follow_up(events[0]) is True


def test_delivery_audit_row_shape_from_selected_and_delivery() -> None:
    selected = normalize_delivery_audit_row(
        {
            "ts": "2026-06-03T10:00:00+00:00",
            "symbol": "ETHUSDT",
            "setup_id": "structure_pullback",
            "status": "selected",
            "source": "selected",
        }
    )
    delivered = normalize_delivery_audit_row(
        {
            "ts": "2026-06-03T10:00:01+00:00",
            "symbol": "ETHUSDT",
            "setup_id": "structure_pullback",
            "delivery_status": "sent",
            "message_id": 12345,
        }
    )

    assert set(selected) == DELIVERY_AUDIT_ROW_KEYS
    assert set(delivered) == DELIVERY_AUDIT_ROW_KEYS
    assert selected["delivery_status"] == "selected"
    assert delivered["delivery_status"] == "sent"
    assert delivered["message_id"] == 12345
