"""Wave E7: REST + WS network probe expansion."""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Self
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bot.domain.config import BotSettings, NetworkConfig
from bot.market.proxy_bootstrap import (
    NetworkProbeResult,
    ensure_network_ready,
    probe_network,
    probe_ws_handshake,
    retry_network_after_failure,
)
from scripts.live_check_binance_api import PUBLIC_FDATA_PATHS, PUBLIC_REST_PATHS


class _FakeWS:
    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None


@asynccontextmanager
async def _fake_ws_connect(*args: Any, **kwargs: Any):
    del args, kwargs
    yield _FakeWS()


@pytest.mark.asyncio
async def test_probe_ws_handshake_success() -> None:
    with patch("bot.market.proxy_bootstrap.websockets.connect", side_effect=_fake_ws_connect):
        assert await probe_ws_handshake(trust_env=False) is True


@pytest.mark.asyncio
async def test_probe_ws_handshake_failure() -> None:
    with patch(
        "bot.market.proxy_bootstrap.websockets.connect",
        side_effect=OSError("connection refused"),
    ):
        assert await probe_ws_handshake(trust_env=False) is False


@pytest.mark.asyncio
async def test_probe_network_reports_rest_and_ws(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _rest_ok(_net: NetworkConfig) -> bool:
        return True

    async def _ws_ok(**kwargs: object) -> bool:
        del kwargs
        return True

    monkeypatch.setattr("bot.market.proxy_bootstrap._probe_rest", _rest_ok)
    monkeypatch.setattr("bot.market.proxy_bootstrap.probe_ws_handshake", _ws_ok)
    result = await probe_network(NetworkConfig(trust_env=False))
    assert result == NetworkProbeResult(rest_ok=True, ws_ok=True)


@pytest.mark.asyncio
async def test_ensure_network_ready_skips_discovery_when_ws_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = BotSettings(tg_token="test", target_chat_id="1")

    async def _direct() -> NetworkProbeResult:
        return NetworkProbeResult(rest_ok=False, ws_ok=True)

    async def _configured(_urls: list[str]) -> NetworkProbeResult:
        return NetworkProbeResult(rest_ok=False, ws_ok=False)

    discovery = MagicMock()
    monkeypatch.setattr("bot.market.proxy_bootstrap._probe_direct", _direct)
    monkeypatch.setattr("bot.market.proxy_bootstrap._probe_configured", _configured)
    monkeypatch.setattr("bot.market.proxy_bootstrap._run_discovery", discovery)

    out = await ensure_network_ready(settings, config_path=Path("config.toml"))
    assert out is settings
    discovery.assert_not_called()


@pytest.mark.asyncio
async def test_ensure_network_ready_runs_discovery_when_both_fail(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text("[bot]\n", encoding="utf-8")
    settings = BotSettings(tg_token="test", target_chat_id="1")

    async def _direct() -> NetworkProbeResult:
        return NetworkProbeResult(rest_ok=False, ws_ok=False)

    async def _configured(_urls: list[str]) -> NetworkProbeResult:
        return NetworkProbeResult(rest_ok=False, ws_ok=False)

    discovery = MagicMock()
    reload = MagicMock(return_value=settings)
    monkeypatch.setattr("bot.market.proxy_bootstrap._probe_direct", _direct)
    monkeypatch.setattr("bot.market.proxy_bootstrap._probe_configured", _configured)
    monkeypatch.setattr("bot.market.proxy_bootstrap._run_discovery", discovery)
    monkeypatch.setattr("bot.market.proxy_bootstrap.load_settings", reload)

    out = await ensure_network_ready(settings, config_path=config_path)
    assert out is settings
    discovery.assert_called_once_with(config_path)
    reload.assert_called_once_with(config_path)


@pytest.mark.asyncio
async def test_retry_network_after_failure_skips_when_rest_ok(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = BotSettings(tg_token="test", target_chat_id="1")

    async def _configured(_urls: list[str]) -> NetworkProbeResult:
        return NetworkProbeResult(rest_ok=True, ws_ok=False)

    async def _direct() -> NetworkProbeResult:
        return NetworkProbeResult(rest_ok=False, ws_ok=True)

    discovery = MagicMock()
    monkeypatch.setattr("bot.market.proxy_bootstrap._probe_configured", _configured)
    monkeypatch.setattr("bot.market.proxy_bootstrap._probe_direct", _direct)
    monkeypatch.setattr("bot.market.proxy_bootstrap._run_discovery", discovery)

    out = await retry_network_after_failure(settings, config_path=Path("config.toml"))
    assert out is settings
    discovery.assert_not_called()


def test_live_check_public_paths_include_futures_data() -> None:
    assert "/futures/data/openInterestHist" in PUBLIC_FDATA_PATHS
    assert "/futures/data/globalLongShortAccountRatio" in PUBLIC_REST_PATHS
    assert "/futures/data/takerlongshortRatio" in PUBLIC_REST_PATHS


@pytest.mark.asyncio
async def test_probe_binance_access_ws_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    from scripts import probe_binance_access

    ws_probe = AsyncMock(return_value=True)
    monkeypatch.setattr(probe_binance_access, "probe_ws_handshake", ws_probe)
    net = NetworkConfig(trust_env=False)
    assert await probe_binance_access._probe_ws_network(net) == 0
    ws_probe.assert_awaited_once()


@pytest.mark.asyncio
async def test_probe_binance_access_both_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    from scripts import probe_binance_access

    monkeypatch.setattr(probe_binance_access, "_probe_rest_network", AsyncMock(return_value=0))
    monkeypatch.setattr(probe_binance_access, "_probe_ws_network", AsyncMock(return_value=0))
    net = NetworkConfig(trust_env=False)
    assert await probe_binance_access._probe_network(net, mode="both") == 0


@pytest.mark.asyncio
async def test_probe_binance_access_both_mode_fails_when_ws_down(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts import probe_binance_access

    monkeypatch.setattr(probe_binance_access, "_probe_rest_network", AsyncMock(return_value=0))
    monkeypatch.setattr(probe_binance_access, "_probe_ws_network", AsyncMock(return_value=2))
    net = NetworkConfig(trust_env=False)
    assert await probe_binance_access._probe_network(net, mode="both") == 2
