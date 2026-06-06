"""Ensure ``[bot.network]`` has a working proxy pool before runtime starts."""

from __future__ import annotations

import asyncio
import logging
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import websockets

from bot.domain.config import BotSettings, NetworkConfig, load_settings
from bot.market.data import BinanceFuturesMarketData
from bot.market.network_proxy import websockets_connect_kwargs
from bot.market.rest_impl import BinanceClientImpl

LOG = logging.getLogger("bot.market.proxy_bootstrap")

_DISCOVER_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "discover_binance_proxies.py"
_BINANCE_WS_HANDSHAKE_URL = "wss://fstream.binance.com/ws"
_WS_PROBE_TIMEOUT_SECONDS = 15.0
_REST_SYMBOL_THRESHOLD = 100


@dataclass(slots=True, frozen=True)
class NetworkProbeResult:
    rest_ok: bool
    ws_ok: bool

    @property
    def any_ok(self) -> bool:
        return self.rest_ok or self.ws_ok


_PROBE_CACHE: dict[str, NetworkProbeResult] = {}
_PROBE_CACHE_MONOTONIC: float = 0.0


def record_network_probe(scope: str, result: NetworkProbeResult) -> None:
    """Store the latest REST/WS probe for health and operator surfaces."""
    global _PROBE_CACHE_MONOTONIC  # noqa: PLW0603
    normalized = str(scope or "").strip().lower()
    if not normalized:
        return
    _PROBE_CACHE[normalized] = result
    _PROBE_CACHE_MONOTONIC = time.monotonic()


def network_probe_status() -> dict[str, bool | None]:
    """Return aggregated probe flags from the last bootstrap or retry probes."""
    direct = _PROBE_CACHE.get("direct")
    configured = _PROBE_CACHE.get("configured")
    if direct is None and configured is None:
        return {"rest_probe_ok": None, "ws_probe_ok": None}
    rest_ok = bool((direct and direct.rest_ok) or (configured and configured.rest_ok))
    ws_ok = bool((direct and direct.ws_ok) or (configured and configured.ws_ok))
    return {"rest_probe_ok": rest_ok, "ws_probe_ok": ws_ok}


def clear_network_probe_cache() -> None:
    """Reset probe cache (tests only)."""
    global _PROBE_CACHE_MONOTONIC  # noqa: PLW0603
    _PROBE_CACHE.clear()
    _PROBE_CACHE_MONOTONIC = 0.0


async def probe_ws_handshake(
    *,
    proxy_url: str | None = None,
    trust_env: bool = True,
    timeout_seconds: float = _WS_PROBE_TIMEOUT_SECONDS,
) -> bool:
    """Open and close a Binance futures public websocket (handshake only)."""
    connect_kwargs = websockets_connect_kwargs(proxy_url=proxy_url, trust_env=trust_env)
    try:
        async with asyncio.timeout(timeout_seconds):
            async with websockets.connect(
                _BINANCE_WS_HANDSHAKE_URL,
                ping_interval=None,
                close_timeout=5.0,
                open_timeout=timeout_seconds,
                **connect_kwargs,
            ):
                return True
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception as exc:  # noqa: BLE001 — proxy libs raise non-standard exceptions
        LOG.debug(
            "ws handshake probe failed | proxy=%s trust_env=%s err=%s",
            proxy_url,
            trust_env,
            exc,
        )
        return False


async def _probe_rest(net: NetworkConfig) -> bool:
    client = BinanceClientImpl(rest_timeout_seconds=12.0, network=net)
    market = BinanceFuturesMarketData(binance_client=client)
    try:
        symbols = await market.fetch_exchange_symbols()
        return len(symbols) > _REST_SYMBOL_THRESHOLD
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception:  # noqa: BLE001
        return False
    finally:
        await market.close()


async def probe_network(net: NetworkConfig) -> NetworkProbeResult:
    """Probe REST exchangeInfo and WS handshake for the given network config."""
    rest_ok, ws_ok = await asyncio.gather(
        _probe_rest(net),
        probe_ws_handshake(proxy_url=net.proxy_url, trust_env=net.trust_env),
    )
    return NetworkProbeResult(rest_ok=rest_ok, ws_ok=ws_ok)


async def _probe_direct() -> NetworkProbeResult:
    net = NetworkConfig(trust_env=False, failover_enabled=False)
    return await probe_network(net)


async def _probe_configured(urls: list[str]) -> NetworkProbeResult:
    if not urls:
        return NetworkProbeResult(rest_ok=False, ws_ok=False)
    net = NetworkConfig(
        proxy_url=urls[0],
        proxy_urls=urls[1:],
        trust_env=False,
        failover_enabled=True,
    )
    return await probe_network(net)


def _log_probe_result(label: str, result: NetworkProbeResult) -> None:
    record_network_probe(label, result)
    LOG.info(
        "network probe | scope=%s rest_ok=%s ws_ok=%s",
        label,
        result.rest_ok,
        result.ws_ok,
    )


_DISCOVERY_FAIL_STREAK = 0
_LAST_DISCOVERY_MONO = 0.0
_DISCOVERY_BASE_INTERVAL_S = 300.0


def _discovery_backoff_seconds() -> float:
    return min(3600.0, _DISCOVERY_BASE_INTERVAL_S * (2 ** min(_DISCOVERY_FAIL_STREAK, 4)))


def _run_discovery(config_path: Path) -> None:
    global _DISCOVERY_FAIL_STREAK, _LAST_DISCOVERY_MONO

    if not _DISCOVER_SCRIPT.is_file():
        LOG.warning("discover script missing | path=%s", _DISCOVER_SCRIPT)
        return
    now = time.monotonic()
    backoff = _discovery_backoff_seconds()
    if now - _LAST_DISCOVERY_MONO < backoff:
        LOG.debug(
            "proxy discovery skipped | reason=backoff remaining_s=%.1f streak=%d",
            backoff - (now - _LAST_DISCOVERY_MONO),
            _DISCOVERY_FAIL_STREAK,
        )
        return
    _LAST_DISCOVERY_MONO = now
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
        _DISCOVERY_FAIL_STREAK += 1
        LOG.warning(
            "proxy discovery exit=%s stderr=%s streak=%d next_backoff_s=%.0f",
            proc.returncode,
            (proc.stderr or "")[:400],
            _DISCOVERY_FAIL_STREAK,
            _discovery_backoff_seconds(),
        )
    else:
        _DISCOVERY_FAIL_STREAK = 0


async def ensure_network_ready(
    settings: BotSettings,
    *,
    config_path: Path | None = None,
    auto_discover: bool = True,
) -> BotSettings:
    """Reload settings after optional proxy discovery when egress is missing or dead."""
    urls = settings.network.effective_proxy_urls()
    direct = await _probe_direct()
    configured = (
        await _probe_configured(urls) if urls else NetworkProbeResult(rest_ok=False, ws_ok=False)
    )
    _log_probe_result("direct", direct)
    if urls:
        _log_probe_result("configured", configured)

    if direct.rest_ok:
        if urls and not configured.rest_ok:
            LOG.warning(
                "direct binance REST ok but configured proxy pool failed probe — using direct egress"
            )
            # Return new settings with proxy cleared so the caller can switch the live REST client.
            net = settings.network.model_copy(update={"proxy_url": None, "proxy_urls": []})
            return settings.model_copy(update={"network": net})
        return settings

    if configured.rest_ok:
        return settings

    if direct.any_ok or configured.any_ok:
        LOG.warning("binance REST blocked but WS reachable - continuing without proxy refresh")
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


async def retry_network_after_failure(
    settings: BotSettings,
    *,
    config_path: Path | None = None,
) -> BotSettings:
    """Re-run proxy discovery when REST paths fail mid-runtime."""
    urls = settings.network.effective_proxy_urls()
    configured = await _probe_configured(urls)
    direct = await _probe_direct()
    _log_probe_result("retry_configured", configured)
    _log_probe_result("retry_direct", direct)
    if configured.rest_ok or direct.rest_ok:
        return settings
    path = config_path or Path("config.toml")
    if not path.is_file():
        return settings
    LOG.warning("binance REST unreachable - re-running proxy discovery")
    await asyncio.to_thread(_run_discovery, path)
    return load_settings(path)
