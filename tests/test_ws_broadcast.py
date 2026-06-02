"""Unit tests for dashboard WebSocket broadcaster."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from bot.core.event_bus import EventBus
from bot.dashboard.live import funnel_stage_counts, funnel_stage_counts_from_cycle
from bot.dashboard.ws_broadcast import DashboardWSBroadcaster, build_ws_health_payload


class _FakeWebSocket:
    def __init__(self) -> None:
        self.messages: list[str] = []

    async def accept(self) -> None:
        return None

    async def send_text(self, message: str) -> None:
        self.messages.append(message)


@pytest.mark.asyncio
async def test_broadcast_funnel_update_and_ws_health() -> None:
    bus = EventBus()
    broadcaster = DashboardWSBroadcaster(bus)
    client = _FakeWebSocket()
    await broadcaster.connect(client)

    ws_manager = MagicMock()
    ws_manager.is_connected.return_value = True
    ws_manager.state_snapshot.return_value = {"stale_kline_stream_count": 2}
    bot = SimpleNamespace(
        _ws_manager=ws_manager,
        telemetry=SimpleNamespace(run_id="run_test"),
    )

    broadcaster.notify_cycle_complete(
        bot,
        symbol="BTCUSDT",
        cycle_row={
            "trigger": "kline_close",
            "detector_runs": 12,
            "candidate_count": 3,
            "selected_count": 2,
            "delivered_count": 1,
        },
        funnel={"raw_hits": 10, "post_filter_candidates": 4},
    )
    await asyncio.sleep(0)

    assert len(client.messages) == 2
    payloads = [json.loads(msg) for msg in client.messages]
    types = {row["type"] for row in payloads}
    assert types == {"funnel_update", "ws_health"}

    funnel_msg = next(row for row in payloads if row["type"] == "funnel_update")
    assert funnel_msg["payload"]["symbol"] == "BTCUSDT"
    assert funnel_msg["payload"]["stages"] == {
        "detected": 10,
        "merged": 4,
        "confluence": 2,
        "tier": 2,
        "delivered": 1,
    }

    health_msg = next(row for row in payloads if row["type"] == "ws_health")
    assert health_msg["payload"]["connected"] is True
    assert health_msg["payload"]["stale_kline_stream_count"] == 2


@pytest.mark.asyncio
async def test_broadcast_skips_when_no_clients() -> None:
    bus = EventBus()
    broadcaster = DashboardWSBroadcaster(bus)
    broadcaster.publish_funnel_update({"stages": {"detected": 1}})
    await asyncio.sleep(0)


def test_funnel_stage_counts_from_live_funnel() -> None:
    stages = funnel_stage_counts(
        {
            "cycle_totals": {
                "detector_runs": 100,
                "candidates": 20,
                "selected": 5,
                "delivered": 2,
            },
            "decisions": {"status_counts": {"signal": 15, "reject": 85}},
        }
    )
    assert stages == {
        "detected": 100,
        "merged": 20,
        "confluence": 15,
        "tier": 5,
        "delivered": 2,
    }


def test_funnel_stage_counts_from_cycle_row() -> None:
    stages = funnel_stage_counts_from_cycle(
        cycle_row={
            "detector_runs": 8,
            "candidate_count": 2,
            "selected_signals": 1,
            "delivered_signals": 1,
        },
        funnel={"raw_hits": 6, "post_filter_candidates": 2},
    )
    assert stages["detected"] == 6
    assert stages["delivered"] == 1


def test_build_ws_health_payload_disconnected() -> None:
    bot = SimpleNamespace(_ws_manager=None)
    payload = build_ws_health_payload(bot)
    assert payload["connected"] is False
    assert payload["stale_kline_stream_count"] == 0


@pytest.mark.asyncio
async def test_publish_signal_schedules_broadcast() -> None:
    bus = EventBus()
    broadcaster = DashboardWSBroadcaster(bus)
    client = _FakeWebSocket()
    await broadcaster.connect(client)

    broadcaster.publish_signal({"symbol": "ETHUSDT", "setup_id": "fvg"})
    await asyncio.sleep(0)

    assert len(client.messages) == 1
    payload = json.loads(client.messages[0])
    assert payload["type"] == "signal"
    assert payload["payload"]["symbol"] == "ETHUSDT"
