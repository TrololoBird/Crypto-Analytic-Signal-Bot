"""Wave E8 agent H: health probes, OI interval, spot docs, CI wrapper, aggTrade freshness."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bot.domain.config import BotSettings, RuntimeConfig, WSConfig
from bot.market.proxy_bootstrap import (
    NetworkProbeResult,
    clear_network_probe_cache,
    network_probe_status,
    record_network_probe,
)
from bot.market.ws import FuturesWSManager
from bot.runtime.health_manager import HealthManager
from bot.runtime.oi_refresh_runner import OIRefreshRunner


@pytest.fixture(autouse=True)
def _clear_probe_cache() -> None:
    clear_network_probe_cache()
    yield
    clear_network_probe_cache()


def test_network_probe_status_empty() -> None:
    assert network_probe_status() == {"rest_probe_ok": None, "ws_probe_ok": None}


def test_network_probe_status_merges_direct_and_configured() -> None:
    record_network_probe("direct", NetworkProbeResult(rest_ok=False, ws_ok=True))
    record_network_probe("configured", NetworkProbeResult(rest_ok=True, ws_ok=False))
    assert network_probe_status() == {"rest_probe_ok": True, "ws_probe_ok": True}


@pytest.mark.asyncio
async def test_health_check_exposes_probe_flags() -> None:
    record_network_probe("direct", NetworkProbeResult(rest_ok=True, ws_ok=False))
    record_network_probe("configured", NetworkProbeResult(rest_ok=False, ws_ok=True))

    bot = MagicMock()
    bot._running = True
    bot._ws_manager = MagicMock()
    bot._ws_manager.is_connected.return_value = True
    bot._ws_manager.state_snapshot.return_value = {"fresh_tickers": 1}
    bot._shortlist = []
    bot._last_kline_event_ts = time.monotonic()
    bot.tracker = MagicMock(_pending_outcomes=[])
    bot._modern_repo = AsyncMock(get_active_signals=AsyncMock(return_value=[]))
    bot.feature_flags = AsyncMock(snapshot=AsyncMock(return_value={}))

    health = await HealthManager(bot).health_check()
    assert health["rest_probe_ok"] is True
    assert health["ws_probe_ok"] is True


def test_runtime_config_oi_refresh_interval_default() -> None:
    runtime = RuntimeConfig()
    assert runtime.oi_refresh_interval_minutes == 30


@pytest.mark.asyncio
async def test_oi_refresh_runner_uses_configured_sleep_interval() -> None:
    bot = MagicMock()
    bot._shutdown = asyncio.Event()
    bot._shortlist_lock = asyncio.Lock()
    bot._shortlist = []
    bot.settings = BotSettings(
        tg_token="test",
        target_chat_id="1",
        runtime=RuntimeConfig(oi_refresh_interval_minutes=7),
    )

    runner = OIRefreshRunner(bot)
    sleep_calls: list[float] = []

    async def _fake_wait_for(_awaitable: object, *, timeout: float) -> None:
        sleep_calls.append(timeout)
        bot._shutdown.set()

    with (
        patch.object(runner, "refresh_once", AsyncMock(return_value=0)),
        patch("bot.runtime.oi_refresh_runner.asyncio.sleep", AsyncMock()),
        patch(
            "bot.runtime.oi_refresh_runner.asyncio.wait_for",
            side_effect=_fake_wait_for,
        ),
    ):
        await runner.run()

    assert sleep_calls == [420.0]


def test_ws_config_agg_trade_freshness_default() -> None:
    ws = WSConfig()
    assert ws.agg_trade_freshness_seconds == 300.0


def test_should_drop_stale_agg_trade_uses_separate_freshness() -> None:
    manager = MagicMock()
    manager._cfg = WSConfig(
        market_ticker_freshness_seconds=30.0,
        agg_trade_freshness_seconds=300.0,
    )
    manager._now_epoch_ms.return_value = 1_000_000.0

    # 60s old aggTrade: keep (under 300s cap)
    assert FuturesWSManager._should_drop_stale_event(manager, "aggTrade", 940_000.0) is False
    # 60s old bookTicker: drop (over 30s cap)
    assert FuturesWSManager._should_drop_stale_event(manager, "bookTicker", 940_000.0) is True


def test_ci_live_check_binance_wrapper_exists() -> None:
    script = Path(__file__).resolve().parents[1] / "scripts" / "ci_live_check_binance.sh"
    assert script.is_file()
    text = script.read_text(encoding="utf-8")
    assert "live_check_binance_api.py" in text
    assert "BOT_DISABLE_HTTP_SERVERS" in text


def test_binance_proxy_ru_documents_spot_weight_budget() -> None:
    doc = Path(__file__).resolve().parents[1] / "docs" / "BINANCE_PROXY_RU.md"
    body = doc.read_text(encoding="utf-8")
    assert "spot_companion" in body
    assert "rate_limit" in body or "weight-budget" in body or "weight" in body
