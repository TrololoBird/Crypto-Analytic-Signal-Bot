"""Ensure ``[bot.network]`` has a working proxy pool before runtime starts."""

from __future__ import annotations

import asyncio
import logging
import subprocess
import sys
from pathlib import Path

from bot.domain.config import BotSettings, NetworkConfig, load_settings
from bot.market.data import BinanceFuturesMarketData, MarketDataUnavailable
from bot.market.rest import BinanceClientImpl

LOG = logging.getLogger("bot.market.proxy_bootstrap")

_DISCOVER_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "discover_binance_proxies.py"


async def _probe_direct() -> bool:
    net = NetworkConfig(trust_env=False, failover_enabled=False)
    client = BinanceClientImpl(rest_timeout_seconds=12.0, network=net)
    market = BinanceFuturesMarketData(binance_client=client)
    try:
        symbols = await market.fetch_exchange_symbols()
        return len(symbols) > 100
    except MarketDataUnavailable:
        return False
    finally:
        await market.close()


async def _probe_configured(urls: list[str]) -> bool:
    if not urls:
        return False
    net = NetworkConfig(
        proxy_url=urls[0],
        proxy_urls=urls[1:],
        trust_env=False,
        failover_enabled=True,
    )
    client = BinanceClientImpl(rest_timeout_seconds=12.0, network=net)
    market = BinanceFuturesMarketData(binance_client=client)
    try:
        symbols = await market.fetch_exchange_symbols()
        return len(symbols) > 100
    except MarketDataUnavailable:
        return False
    finally:
        await market.close()


def _run_discovery(config_path: Path) -> None:
    if not _DISCOVER_SCRIPT.is_file():
        LOG.warning("discover script missing | path=%s", _DISCOVER_SCRIPT)
        return
    LOG.info("running proxy discovery | config=%s", config_path)
    proc = subprocess.run(
        [sys.executable, str(_DISCOVER_SCRIPT), "--config", str(config_path)],
        cwd=str(config_path.parent),
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )
    if proc.returncode not in {0, 2}:
        LOG.warning(
            "proxy discovery exit=%s stderr=%s",
            proc.returncode,
            (proc.stderr or "")[:400],
        )


async def ensure_network_ready(
    settings: BotSettings,
    *,
    config_path: Path | None = None,
    auto_discover: bool = True,
) -> BotSettings:
    """Reload settings after optional proxy discovery when egress is missing or dead."""
    urls = settings.network.effective_proxy_urls()
    direct_ok = await _probe_direct()
    configured_ok = await _probe_configured(urls) if urls else False

    if direct_ok or configured_ok:
        return settings

    if not auto_discover:
        LOG.error("binance unreachable and auto_discover disabled")
        return settings

    path = config_path or Path("config.toml")
    if path.is_file():
        await asyncio.to_thread(_run_discovery, path)
        return load_settings(path)

    LOG.error("binance unreachable | config missing for discovery")
    return settings
