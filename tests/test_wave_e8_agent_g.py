"""Wave E8 agent G: buffer alerts, liquidation enrich, CVD, telemetry, health."""

from __future__ import annotations

import asyncio
import time
from collections import deque
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import polars as pl
import pytest

from bot.domain.config import BotSettings, WSConfig
from bot.features.prepare import _enrich_with_ws_data
from bot.features.prepare_frame import add_session_cvd
from bot.market.subscription_planner import ORDER_FLOW_ANCHOR_SYMBOLS, SubscriptionBudget
from bot.market.ws import FuturesWSManager, get_liquidation_rollups
from bot.runtime.delivery_alerts import (
    _message_buffer_dropped_total,
    check_message_buffer_drop_alert,
)
from bot.runtime.health_manager import HealthManager
from bot.telemetry import TelemetryStore


def _two_day_flow_frame() -> pl.DataFrame:
    times = [
        datetime(2026, 6, 1, 23, 45, tzinfo=UTC),
        datetime(2026, 6, 2, 0, 0, tzinfo=UTC),
    ]
    return pl.DataFrame(
        {
            "open_time": times,
            "close_time": times,
            "open": [1.0, 1.0],
            "high": [1.0, 1.0],
            "low": [1.0, 1.0],
            "close": [1.0, 1.0],
            "volume": [100.0, 100.0],
            "taker_buy_base_volume": [70.0, 70.0],
            "delta_ratio": [0.7, 0.7],
        }
    )


class _FakeLiquidationManager:
    def __init__(self) -> None:
        now_ms = int(time.time() * 1000)
        self._force_order_buffer: deque[tuple[int, str, str, float, float]] = deque(
            [
                (now_ms - 1000, "BTCUSDT", "SELL", 2.0, 100_000.0),
                (now_ms - 2000, "BTCUSDT", "BUY", 1.0, 100_000.0),
            ]
        )

    def get_liquidation_rollups(
        self,
        symbol: str | None = None,
        window_seconds: int = 900,
    ) -> dict[str, float] | None:
        return get_liquidation_rollups(self, symbol=symbol, window_seconds=window_seconds)


def _bot(*, drop_threshold: int = 100) -> SimpleNamespace:
    settings = BotSettings(
        tg_token="t",
        target_chat_id="1",
        ws=WSConfig(message_buffer_drop_alert_threshold=drop_threshold),
    )
    return SimpleNamespace(
        settings=settings,
        _message_buffer_drop_baseline_set=False,
        _last_message_buffer_dropped=0,
        _last_message_buffer_drop_alert_mono=0.0,
    )


def test_message_buffer_dropped_total_reads_nested_stats() -> None:
    total = _message_buffer_dropped_total({"message_buffer": {"dropped": 42, "size": 3}})
    assert total == 42


@pytest.mark.asyncio
async def test_message_buffer_drop_alert_sets_baseline_first() -> None:
    bot = _bot(drop_threshold=10)
    with patch(
        "bot.runtime.delivery_alerts.send_ops_webhook_alert",
        new=AsyncMock(return_value=False),
    ):
        await check_message_buffer_drop_alert(
            bot,  # type: ignore[arg-type]
            ws_snapshot={"message_buffer": {"dropped": 500}},
        )
    assert bot._message_buffer_drop_baseline_set is True
    assert bot._last_message_buffer_dropped == 500


@pytest.mark.asyncio
async def test_message_buffer_drop_alert_fires_on_delta() -> None:
    bot = _bot(drop_threshold=50)
    bot._message_buffer_drop_baseline_set = True
    bot._last_message_buffer_dropped = 100
    webhook = AsyncMock(return_value=True)
    with patch("bot.runtime.delivery_alerts.send_ops_webhook_alert", new=webhook):
        await check_message_buffer_drop_alert(
            bot,  # type: ignore[arg-type]
            ws_snapshot={"message_buffer": {"dropped": 200, "size": 12}},
        )
    webhook.assert_awaited_once()
    assert bot._last_message_buffer_dropped == 200


def test_session_cvd_resets_on_utc_date_boundary() -> None:
    out = add_session_cvd(_two_day_flow_frame())
    cvd = out["session_cvd"].to_list()
    assert cvd[0] == pytest.approx(40.0)
    assert cvd[1] == pytest.approx(40.0)


def test_enrich_with_ws_data_merges_liquidation_notionals() -> None:
    frame = pl.DataFrame(
        {
            "close": [100.0],
            "volume": [1000.0],
            "delta_ratio": [0.5],
        }
    )
    manager = _FakeLiquidationManager()
    out = _enrich_with_ws_data(frame, "BTCUSDT", manager)
    assert out["liquidation_long_notional"][0] == pytest.approx(200_000.0)
    assert out["liquidation_short_notional"][0] == pytest.approx(100_000.0)
    assert out["liquidation_score"][0] == pytest.approx(-100_000.0 / 300_000.0)


def test_telemetry_store_has_no_persist_candles() -> None:
    assert not hasattr(TelemetryStore, "persist_candles")


def test_ws_state_snapshot_order_flow_fields() -> None:

    manager = FuturesWSManager.__new__(FuturesWSManager)
    manager._symbols = ["btcusdt", "ethusdt"]
    manager._tracked_symbols = ["btcusdt", "ethusdt", "solusdt"]
    manager._cfg = WSConfig()
    manager._ticker_cache_ts = 0.0
    manager._ticker_cache = {}
    manager._ticker_update_times = {}
    manager._mark_price_update_times = {}
    manager._mark_price_cache = {}
    manager._book_update_times = {}
    manager._depth_update_times = {}
    manager._connected_urls = {"public": None, "market": None}
    manager._connected_endpoints = {
        "public": asyncio.Event(),
        "market": asyncio.Event(),
    }
    manager._connect_count = 0
    manager._last_event_lag_ms = None
    manager._stream_latency_ms = {}
    manager._message_buffer = MagicMock(get_stats=MagicMock(return_value={"size": 0, "dropped": 0}))
    manager._stale_event_drop_count = 0
    manager._connect_counts = {"public": 0, "market": 0}
    manager._force_order_buffer = deque()
    manager._subscription_errors = {"public": None, "market": None}
    manager._subscription_ack_count = {"public": 0, "market": 0}
    manager._last_shortlist_rebuild_ts = 0.0
    manager._intended_streams = set()
    manager._subscription_budget = SubscriptionBudget(
        kline_streams=6,
        book_ticker_streams=2,
        depth_streams=2,
        agg_trade_streams=2,
        global_streams=3,
        total_market=11,
        total_public=4,
        depth_symbols=("btcusdt", "ethusdt"),
        agg_trade_symbols=("btcusdt", "ethusdt"),
        budget_limit=280,
    )
    manager._get_current_latency_ms = MagicMock(return_value=0.0)
    manager._last_message_age_seconds = MagicMock(return_value=0.0)
    manager._is_interval_fresh = MagicMock(return_value=True)
    manager._stale_kline_streams = MagicMock(return_value=[])
    manager.is_warm = MagicMock(return_value=True)

    snap = manager.state_snapshot()
    assert snap["order_flow_tracked_count"] == 3
    assert snap["anchor_symbols_in_agg_trade"] == len(ORDER_FLOW_ANCHOR_SYMBOLS)


@pytest.mark.asyncio
async def test_health_check_exposes_order_flow_fields() -> None:
    bot = MagicMock()
    bot._running = True
    bot._shortlist = []
    bot._last_kline_event_ts = 0.0
    bot.settings = BotSettings(tg_token="t", target_chat_id="1")
    bot.tracker = SimpleNamespace(_pending_outcomes=[])
    bot._modern_repo = AsyncMock()
    bot._modern_repo.get_active_signals.return_value = []
    bot.feature_flags = AsyncMock()
    bot.feature_flags.snapshot.return_value = {}
    bot._ws_manager = MagicMock()
    bot._ws_manager.is_connected.return_value = True
    bot._ws_manager.state_snapshot.return_value = {
        "fresh_tickers": 1,
        "fresh_mark_prices": 1,
        "order_flow_tracked_count": 5,
        "anchor_symbols_in_agg_trade": 2,
        "stale_kline_stream_count": 0,
    }

    health = await HealthManager(bot).health_check()
    assert health["order_flow_tracked_count"] == 5
    assert health["anchor_symbols_in_agg_trade"] == 2
