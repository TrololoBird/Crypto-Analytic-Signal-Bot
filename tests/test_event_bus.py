from __future__ import annotations

import asyncio

import pytest

from bot.core.event_bus import EventBus
from bot.core.events import BookTickerEvent, KlineCloseEvent, OIRefreshDueEvent, ShortlistUpdatedEvent


@pytest.mark.asyncio
async def test_event_bus_coalesces_hot_path_events_and_reports_stats() -> None:
    bus = EventBus(max_size=16, warn_depth=8, drop_log_interval=1)
    seen: dict[str, object] = {}

    async def on_kline(event: KlineCloseEvent) -> None:
        seen["kline"] = event

    async def on_book(event: BookTickerEvent) -> None:
        seen["book"] = event

    async def on_shortlist(event: ShortlistUpdatedEvent) -> None:
        seen["shortlist"] = event

    async def on_oi(event: OIRefreshDueEvent) -> None:
        seen["oi"] = event

    bus.subscribe(KlineCloseEvent, on_kline)
    bus.subscribe(BookTickerEvent, on_book)
    bus.subscribe(ShortlistUpdatedEvent, on_shortlist)
    bus.subscribe(OIRefreshDueEvent, on_oi)

    task = asyncio.create_task(bus.run())
    try:
        await bus.publish(KlineCloseEvent(symbol="BTCUSDT", interval="15m", close_ts=1))
        await bus.publish(KlineCloseEvent(symbol="BTCUSDT", interval="15m", close_ts=2))
        await bus.publish(BookTickerEvent(symbol="BTCUSDT", bid=100.0, ask=100.1))
        await bus.publish(BookTickerEvent(symbol="BTCUSDT", bid=101.0, ask=101.1))
        await bus.publish(ShortlistUpdatedEvent(symbols=("BTCUSDT",)))
        await bus.publish(ShortlistUpdatedEvent(symbols=("ETHUSDT", "BTCUSDT")))
        await bus.publish(OIRefreshDueEvent(symbols=("BTCUSDT",)))
        await bus.publish(OIRefreshDueEvent(symbols=("ETHUSDT",)))
        await asyncio.sleep(0.05)

        assert isinstance(seen["kline"], KlineCloseEvent)
        assert seen["kline"].close_ts == 2
        assert isinstance(seen["book"], BookTickerEvent)
        assert seen["book"].bid == pytest.approx(101.0)
        assert isinstance(seen["shortlist"], ShortlistUpdatedEvent)
        assert seen["shortlist"].symbols == ("ETHUSDT", "BTCUSDT")
        assert isinstance(seen["oi"], OIRefreshDueEvent)
        assert seen["oi"].symbols == ("ETHUSDT",)

        stats = bus.stats()
        assert stats["current_depth"] == 0
        assert stats["high_water_mark"] >= 1
        assert stats["coalesced_count"] >= 4
        assert stats["dropped_count"] == 0
    finally:
        task.cancel()
        await task


@pytest.mark.asyncio
async def test_event_bus_evicts_oldest_same_type_when_bounded_queue_is_full() -> None:
    bus = EventBus(max_size=1, warn_depth=1, drop_log_interval=1)
    seen: list[KlineCloseEvent] = []

    async def on_kline(event: KlineCloseEvent) -> None:
        seen.append(event)

    bus.subscribe(KlineCloseEvent, on_kline)
    await bus.publish(KlineCloseEvent(symbol="BTCUSDT", interval="15m", close_ts=1))
    await bus.publish(KlineCloseEvent(symbol="ETHUSDT", interval="15m", close_ts=2))

    task = asyncio.create_task(bus.run())
    try:
        await asyncio.sleep(0.05)
        assert [event.symbol for event in seen] == ["ETHUSDT"]
        stats = bus.stats()
        assert stats["dropped_count"] == 1
        assert stats["coalesced_count"] == 0
    finally:
        task.cancel()
        await task
