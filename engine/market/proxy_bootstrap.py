"""Autonomous proxy management: discover → validate → connect → rotate.

On startup ``ensure_network_ready`` is called.  If direct Binance egress
works and no explicit proxy_url is set, auto-discovered pools are cleared
so both REST and WS go direct.  If blocked, the module discovers proxies
from 20+ public no-auth lists, validates against the real Binance fstream
endpoint with 80 concurrent workers, keeps fastest working proxies, and
hot-swaps the live pool without restarting.

Tor circuit rotation: when a local Tor daemon + stem library are available,
``tor_rotate_circuit()`` sends NEWNYM every refresh cycle for a fresh exit IP.

Source list constants live in ``_proxy_sources.py``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass
from pathlib import Path

import aiohttp
import tomlkit
import websockets

from engine.domain.config import BotSettings, NetworkConfig
from engine.errors import DEFENSIVE_EXC, defensive_exc_types
from engine.market._proxy_sources import (
    _BINANCE_PING_URL,
    _BINANCE_WS_HANDSHAKE_URL,
    _HTTP_SOURCES,
    _MAX_CANDIDATES,
    _MAX_POOL_SIZE,
    _MIN_WORKING_PROXIES,
    _POOL_REFRESH_INTERVAL_S,
    _SOCKS5_SOURCES,
    _VALIDATE_CONCURRENCY,
    _VALIDATE_TIMEOUT_S,
    _WS_PROBE_TIMEOUT_SECONDS,
    _gather_candidates,
    _load_custom_sources,
)
from engine.market.network_proxy import websockets_connect_kwargs

try:
    import stem
    import stem.control
except ImportError:
    stem = None

try:
    from aiohttp_socks import ProxyConnector
except ImportError:
    ProxyConnector = None

LOG = logging.getLogger("bot.market.proxy_bootstrap")


# ---------------------------------------------------------------------------
# Probe cache
# ---------------------------------------------------------------------------


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
    global _PROBE_CACHE_MONOTONIC  # noqa: PLW0603
    normalized = str(scope or "").strip().lower()
    if not normalized:
        return
    _PROBE_CACHE[normalized] = result
    _PROBE_CACHE_MONOTONIC = time.monotonic()


def network_probe_status() -> dict[str, bool | None]:
    direct = _PROBE_CACHE.get("direct")
    configured = _PROBE_CACHE.get("configured")
    if direct is None and configured is None:
        return {"rest_probe_ok": None, "ws_probe_ok": None}
    rest_ok = bool((direct and direct.rest_ok) or (configured and configured.rest_ok))
    ws_ok = bool((direct and direct.ws_ok) or (configured and configured.ws_ok))
    return {"rest_probe_ok": rest_ok, "ws_probe_ok": ws_ok}


def clear_network_probe_cache() -> None:
    global _PROBE_CACHE_MONOTONIC  # noqa: PLW0603
    _PROBE_CACHE.clear()
    _PROBE_CACHE_MONOTONIC = 0.0


# ---------------------------------------------------------------------------
# Basic connectivity probes (direct egress + configured pool)
# ---------------------------------------------------------------------------


async def probe_ws_handshake(
    *,
    proxy_url: str | None = None,
    trust_env: bool = True,
    timeout_seconds: float = _WS_PROBE_TIMEOUT_SECONDS,
) -> bool:
    connect_kwargs = websockets_connect_kwargs(proxy_url=proxy_url, trust_env=trust_env)
    try:
        async with asyncio.timeout(timeout_seconds):
            async with websockets.connect(
                _BINANCE_WS_HANDSHAKE_URL,
                ping_interval=None,
                close_timeout=5.0,
                open_timeout=timeout_seconds,
                additional_headers={"User-Agent": "python-websockets/binance-bot"},
                **connect_kwargs,
            ):
                return True
    except (KeyboardInterrupt, SystemExit):
        raise
    except (
        OSError,
        ConnectionError,
        TimeoutError,
        websockets.exceptions.WebSocketException,
    ) as exc:
        LOG.debug("ws handshake probe failed | proxy=%s err=%s", proxy_url, exc)
        return False


async def _probe_rest(net: NetworkConfig) -> bool:
    """Probe Binance REST ping directly without creating a full client."""
    proxy_url = str(net.proxy_url or "").strip() or None
    is_socks = bool(proxy_url and proxy_url.startswith("socks"))
    try:
        async with asyncio.timeout(12.0):
            if is_socks:
                if ProxyConnector is None:
                    return False
                connector = ProxyConnector.from_url(proxy_url, rdns=True)
                async with (
                    aiohttp.ClientSession(connector=connector) as sess,
                    sess.get(_BINANCE_PING_URL, ssl=True) as resp,
                ):
                    status: int = resp.status
                    return status == 200
            else:
                async with (
                    aiohttp.ClientSession() as sess,
                    sess.get(_BINANCE_PING_URL, proxy=proxy_url, ssl=True) as resp,
                ):
                    status = resp.status
                    return status == 200
    except defensive_exc_types(aiohttp.ClientError):
        return False


async def probe_network(net: NetworkConfig) -> NetworkProbeResult:
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

    async def _probe_one(url: str) -> NetworkProbeResult:
        net = NetworkConfig(
            proxy_url=url,
            proxy_urls=[],
            trust_env=False,
            failover_enabled=False,
        )
        return await probe_network(net)

    results = await asyncio.gather(*[_probe_one(u) for u in urls], return_exceptions=True)
    rest_ok = any(isinstance(r, NetworkProbeResult) and r.rest_ok for r in results)
    ws_ok = any(isinstance(r, NetworkProbeResult) and r.ws_ok for r in results)
    return NetworkProbeResult(rest_ok=rest_ok, ws_ok=ws_ok)


def _log_probe_result(label: str, result: NetworkProbeResult) -> None:
    record_network_probe(label, result)
    LOG.info(
        "network probe | scope=%s rest_ok=%s ws_ok=%s",
        label,
        result.rest_ok,
        result.ws_ok,
    )


# ---------------------------------------------------------------------------
# Autonomous proxy discovery
# ---------------------------------------------------------------------------


async def _detect_local_tor() -> str | None:
    """Return socks5://127.0.0.1:9050 if a local Tor SOCKS daemon is reachable."""
    try:
        async with asyncio.timeout(2.0):
            _r, writer = await asyncio.open_connection("127.0.0.1", 9050)
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()
    except (OSError, ConnectionRefusedError, TimeoutError):
        return None
    else:
        LOG.info("local Tor daemon detected on 127.0.0.1:9050 — adding to pool")
        return "socks5://127.0.0.1:9050"


async def tor_rotate_circuit(*, control_port: int = 9051, timeout_s: float = 15.0) -> bool:
    """
    Signal Tor to build a fresh circuit (new exit IP) via NEWNYM.

    Requires:
    - Tor daemon running with ControlPort enabled (torrc: ``ControlPort 9051``)
    - ``stem`` library installed (``pip install stem``)
    - CookieAuthentication or no auth (default for most local installs)

    Returns True on success, False if stem is unavailable or control port unreachable.
    Used by ``run_proxy_refresh_loop`` to rotate the Tor exit IP every refresh cycle.
    """
    if stem is None:
        return False

    loop = asyncio.get_event_loop()

    def _do_rotate() -> bool:
        try:
            with stem.control.Controller.from_port(port=control_port) as ctrl:
                ctrl.authenticate()
                wait = ctrl.get_newnym_wait()
                if wait > 0:
                    LOG.debug("Tor NEWNYM cooldown %.1fs — waiting", wait)
                    time.sleep(min(wait, timeout_s * 0.8))
                ctrl.signal(stem.Signal.NEWNYM)
                return True
        except (OSError, ConnectionError, ValueError, AttributeError) as exc:
            LOG.debug("Tor circuit rotation failed: %s", exc)
            return False

    try:
        async with asyncio.timeout(timeout_s):
            result: bool = await loop.run_in_executor(None, _do_rotate)
            if result:
                LOG.info("Tor circuit rotated via NEWNYM — fresh exit IP active")
            return result
    except TimeoutError:
        LOG.debug("Tor circuit rotation timed out after %.1fs", timeout_s)
        return False


async def _validate_proxy_binance(
    proxy_url: str,
    sem: asyncio.Semaphore,
    timeout_s: float = _VALIDATE_TIMEOUT_S,
) -> tuple[str, float] | None:
    """
    Validate *proxy_url* against the real Binance fstream ping endpoint.
    Supports SOCKS5 (via aiohttp_socks) and HTTP CONNECT proxies.
    Returns ``(proxy_url, latency_ms)`` on success, ``None`` on any failure.
    """
    is_socks = proxy_url.startswith("socks")
    if is_socks and ProxyConnector is None:
        return None

    async with sem:
        connector: aiohttp.BaseConnector | None = None
        try:
            t0 = time.monotonic()
            async with asyncio.timeout(timeout_s):
                if is_socks:
                    connector = ProxyConnector.from_url(proxy_url, rdns=True)
                    async with (
                        aiohttp.ClientSession(connector=connector) as sess,
                        sess.get(_BINANCE_PING_URL, ssl=True) as resp,
                    ):
                        if resp.status == 200:
                            return proxy_url, (time.monotonic() - t0) * 1000.0
                else:
                    connector = aiohttp.TCPConnector()
                    async with (
                        aiohttp.ClientSession(connector=connector) as sess,
                        sess.get(_BINANCE_PING_URL, proxy=proxy_url, ssl=True) as resp,
                    ):
                        if resp.status == 200:
                            return proxy_url, (time.monotonic() - t0) * 1000.0
        except defensive_exc_types(aiohttp.ClientError):
            pass
        finally:
            if connector is not None and not connector.closed:
                connector.close()
    return None


async def _validate_batch(
    candidates: list[str],
    *,
    validate_timeout: float,
    validate_concurrency: int,
) -> list[tuple[str, float]]:
    """Validate a list of proxy candidates against Binance and return working ones."""
    sem = asyncio.Semaphore(validate_concurrency)
    val_results = await asyncio.gather(
        *[_validate_proxy_binance(u, sem, validate_timeout) for u in candidates],
        return_exceptions=True,
    )
    return [r for r in val_results if isinstance(r, tuple)]


async def auto_discover_proxies(
    *,
    max_pool_size: int = _MAX_POOL_SIZE,
    validate_timeout: float = _VALIDATE_TIMEOUT_S,
    validate_concurrency: int = _VALIDATE_CONCURRENCY,
) -> list[str]:
    """
    Discover working proxies from 20+ public no-auth sources.

    Strategy:
    1. Probe local Tor daemon (127.0.0.1:9050) — zero-cost, fast
    2. Fetch and validate SOCKS5 from 16 GitHub/API sources in parallel
    3. If pool still insufficient, fetch HTTP proxies as fallback
    4. Return up to *max_pool_size* URLs sorted by ascending latency (fastest first)
    """
    # Merge custom proxy source URLs before discovery
    custom = _load_custom_sources()
    if custom:
        LOG.info("proxy auto-discovery: %d custom sources added", len(custom))

    LOG.info(
        "proxy auto-discovery: %d SOCKS5 sources + %d HTTP fallback + Tor detection",
        len(_SOCKS5_SOURCES),
        len(_HTTP_SOURCES),
    )

    # 1. Detect local Tor in parallel with source fetching
    tor_task: asyncio.Task[str | None] = asyncio.create_task(_detect_local_tor())

    # 2. Fetch SOCKS5 candidates from all sources (including custom)
    socks5_sources = list(_SOCKS5_SOURCES) + custom
    candidates = await _gather_candidates(socks5_sources, protocol="socks5")
    candidates = candidates[:_MAX_CANDIDATES]

    LOG.info(
        "proxy auto-discovery: %d unique SOCKS5 candidates — validating...",
        len(candidates),
    )

    working: list[tuple[str, float]] = []
    if candidates:
        working = await _validate_batch(
            candidates,
            validate_timeout=validate_timeout,
            validate_concurrency=validate_concurrency,
        )

    LOG.info(
        "proxy auto-discovery SOCKS5: %d/%d reached Binance",
        len(working),
        len(candidates),
    )

    # 3. HTTP fallback when SOCKS5 pool is insufficient
    if len(working) < _MIN_WORKING_PROXIES:
        LOG.warning(
            "SOCKS5 pool insufficient (%d) — fetching HTTP fallback sources",
            len(working),
        )
        http_sources = list(_HTTP_SOURCES) + custom
        http_candidates = await _gather_candidates(http_sources, protocol="http")
        http_candidates = http_candidates[:_MAX_CANDIDATES]
        if http_candidates:
            http_working = await _validate_batch(
                http_candidates,
                validate_timeout=validate_timeout,
                validate_concurrency=validate_concurrency,
            )
            LOG.info(
                "proxy auto-discovery HTTP fallback: %d/%d reached Binance",
                len(http_working),
                len(http_candidates),
            )
            working.extend(http_working)

    # 4. Inject local Tor if available
    tor_url = await tor_task
    if tor_url:
        # Validate Tor too (it may be slow but reliable)
        sem = asyncio.Semaphore(1)
        tor_result = await _validate_proxy_binance(tor_url, sem, validate_timeout)
        if tor_result:
            working.insert(0, tor_result)  # Tor goes first — most reliable from RU
            LOG.info("Tor SOCKS5 validated and added as primary proxy")

    if not working:
        LOG.error(
            "proxy auto-discovery: 0 working proxies from all %d sources",
            len(_SOCKS5_SOURCES) + len(_HTTP_SOURCES),
        )
        return []

    working.sort(key=lambda x: x[1])
    best = [url for url, _ in working[:max_pool_size]]

    LOG.info(
        "proxy auto-discovery done | validated=%d best_ms=%.0f worst_ms=%.0f pool=%d",
        len(working),
        working[0][1],
        working[min(len(best) - 1, len(working) - 1)][1],
        len(best),
    )
    return best


def _write_proxies_to_config(path: Path, urls: list[str]) -> None:
    """
    Persist *urls* into config.toml via tomlkit (preserves comments/formatting).
    Updates ``proxy_url`` and ``proxy_urls`` in the ``[bot.network]`` section.
    """
    if not path.is_file() or not urls:
        return

    document: tomlkit.TOMLDocument = tomlkit.parse(path.read_text(encoding="utf-8"))
    network = document.get("bot", tomlkit.table()).get("network", tomlkit.table())

    network["proxy_url"] = urls[0]
    network["proxy_urls"] = urls[1:]

    path.write_text(tomlkit.dumps(document), encoding="utf-8")
    LOG.info(
        "proxy config persisted | path=%s primary=%s pool_size=%d",
        path,
        urls[0],
        len(urls),
    )


# ---------------------------------------------------------------------------
# Startup gate
# ---------------------------------------------------------------------------


async def ensure_network_ready(
    settings: BotSettings,
    *,
    _config_path: Path | None = None,
    auto_discover: bool = True,
) -> BotSettings:
    """
    Probe Binance reachability.  If blocked, auto-discover working SOCKS5
    proxies from public sources and return updated settings.
    The result is persisted to config.toml so the next startup is warm.
    """
    urls = settings.network.effective_proxy_urls()
    direct = await _probe_direct()
    configured = (
        await _probe_configured(urls) if urls else NetworkProbeResult(rest_ok=False, ws_ok=False)
    )
    _log_probe_result("direct", direct)
    if urls:
        _log_probe_result("configured", configured)

    if direct.rest_ok:
        explicit_proxy = str(settings.network.proxy_url or "").strip()
        if not explicit_proxy:
            # No explicitly configured proxy_url — auto-discovered pool should not override
            # direct access.  Clear the pool AND disable trust_env so the main aiohttp session
            # does not pick up broken system proxies (Shadowsocks/Clash/V2rayU on macOS).
            # The direct probe already confirmed access works with trust_env=False.
            if urls or settings.network.trust_env:
                LOG.info(
                    "direct Binance ok, no explicit proxy_url — using pure direct egress "
                    "(trust_env=False, pool cleared, %d auto-discovered entries dropped)",
                    len(urls),
                )
            net = settings.network.model_copy(
                update={
                    "proxy_url": None,
                    "proxy_urls": [],
                    "failover_enabled": False,
                    "trust_env": False,
                }
            )
            return settings.model_copy(update={"network": net})
        # Explicit proxy_url configured — honour it unless it fails
        if urls and not configured.rest_ok:
            LOG.warning(
                "direct Binance ok but configured proxy failed — switching to direct egress"
            )
            net = settings.network.model_copy(update={"proxy_url": None, "proxy_urls": []})
            return settings.model_copy(update={"network": net})
        return settings

    if configured.rest_ok:
        return settings

    if direct.any_ok or configured.any_ok:
        LOG.warning("Binance REST blocked but WS reachable — continuing, will retry via refresh")
        return settings

    if not auto_discover:
        LOG.error("Binance unreachable and auto_discover=False — starting degraded")
        return settings

    LOG.warning("Binance unreachable via direct and configured pool — running autonomous discovery")
    best = await auto_discover_proxies()

    if len(best) < _MIN_WORKING_PROXIES:
        LOG.error(
            "auto-discovery found %d proxies (need %d) — starting degraded",
            len(best),
            _MIN_WORKING_PROXIES,
        )
        return settings

    # Build in-memory settings with discovered proxies (no config.toml write needed)
    net = settings.network.model_copy(
        update={"proxy_url": best[0], "proxy_urls": best[1:], "failover_enabled": True}
    )
    return settings.model_copy(update={"network": net})


# ---------------------------------------------------------------------------
# Runtime: background pool refresh loop
# ---------------------------------------------------------------------------


async def run_proxy_refresh_loop(
    bot: object,
    *,
    interval_seconds: float = _POOL_REFRESH_INTERVAL_S,
    _config_path: Path | None = None,
    shutdown_event: asyncio.Event | None = None,
) -> None:
    """
    Background task — re-discover working proxies every *interval_seconds*
    and hot-swap the live pool without restarting the bot.

    Accepts an optional *shutdown_event* to signal graceful termination;
    falls back to ``bot._shutdown`` if not provided.

    Accesses from *bot* (SignalBot):
      ``client._binance_client`` — live BinanceClientImpl
      ``ws_manager``             — live FuturesWSManager
      ``_shutdown``              — asyncio.Event (fallback)
    """
    shutdown: asyncio.Event = (
        shutdown_event if shutdown_event is not None else getattr(bot, "_shutdown", asyncio.Event())
    )
    LOG.info("proxy refresh loop started | interval=%.0fs", interval_seconds)

    while not shutdown.is_set():
        try:
            await asyncio.wait_for(shutdown.wait(), timeout=interval_seconds)
            break  # shutdown fired
        except TimeoutError:
            pass

        if shutdown.is_set():
            break

        LOG.info("proxy refresh: cycle starting")

        # If direct Binance egress works and no explicit proxy is configured,
        # do not inject an auto-discovered pool.
        direct_probe = await _probe_direct()
        bot_settings = getattr(bot, "settings", None)
        explicit_proxy = str(
            getattr(getattr(bot_settings, "network", None), "proxy_url", None) or ""
        ).strip()
        if direct_probe.rest_ok and not explicit_proxy:
            LOG.info("proxy refresh: direct Binance ok, no explicit proxy_url — skipping")
            continue

        if shutdown.is_set():
            break

        # Rotate Tor circuit first (new exit IP)
        tor_rotated = await tor_rotate_circuit()

        # Step 1 — fast path: re-validate existing pool members
        rest_client = getattr(bot, "client", None)
        inner = getattr(rest_client, "_binance_client", None)
        pool = getattr(inner, "_proxy_pool", None) if inner is not None else None

        removed = 0
        if pool is not None and pool.urls:
            removed = await pool.revalidate(
                _validate_proxy_binance,
                concurrency=min(_VALIDATE_CONCURRENCY, 30),
                timeout_s=_VALIDATE_TIMEOUT_S,
            )
            if removed:
                LOG.info("proxy refresh: revalidate removed %d dead proxies", removed)

        if shutdown.is_set():
            break

        # Step 2 — top-up or full discovery if pool is too small
        pool_size = len(pool.urls) if pool is not None else 0
        if pool_size >= _MIN_WORKING_PROXIES:
            LOG.info(
                "proxy refresh: revalidate good, pool has %d proxies — keeping current",
                pool_size,
            )
            continue

        if shutdown.is_set():
            break

        try:
            best = await auto_discover_proxies()
        except DEFENSIVE_EXC as exc:
            LOG.warning("proxy refresh: discovery error | %s", exc)
            continue

        if shutdown.is_set():
            break

        if len(best) < _MIN_WORKING_PROXIES:
            LOG.warning(
                "proxy refresh: only %d proxies found — keeping current pool (%d)",
                len(best),
                pool_size,
            )
            continue

        # Persist discovered proxies to config.toml for next startup
        config_path: Path | None = getattr(bot, "_config_path", None) or _config_path
        if config_path is not None:
            try:
                await asyncio.to_thread(_write_proxies_to_config, config_path, best)
            except DEFENSIVE_EXC as exc:
                LOG.warning("proxy refresh: config persist failed | %s", exc)

        # Update live REST client pool
        if inner is not None:
            try:
                if hasattr(inner, "refresh_proxy_pool"):
                    await inner.refresh_proxy_pool(best)
                else:
                    if pool is not None:
                        pool.reset(best)
                    await inner._apply_active_proxy(best[0])
                    inner._last_failover_mono = 0.0
                new_pool = getattr(inner, "_proxy_pool", None)
                if new_pool is not None:
                    new_pool.log_metrics("refresh")
            except DEFENSIVE_EXC as exc:
                LOG.warning("proxy refresh: REST client update failed | %s", exc)

        # Update WS manager
        ws_mgr = getattr(bot, "ws_manager", None)
        if ws_mgr is not None and hasattr(ws_mgr, "update_proxy_url"):
            try:
                ws_mgr.update_proxy_url(best[0])
            except DEFENSIVE_EXC as exc:
                LOG.warning("proxy refresh: WS proxy update failed | %s", exc)

        LOG.info(
            "proxy refresh complete | active=%s pool_size=%d tor_rotated=%s",
            best[0],
            len(best),
            tor_rotated,
        )

    LOG.info("proxy refresh loop stopped")


async def retry_network_after_failure(
    settings: BotSettings,
    *,
    _config_path: Path | None = None,
) -> BotSettings:
    """Re-run discovery when REST paths fail mid-runtime."""
    urls = settings.network.effective_proxy_urls()
    configured = await _probe_configured(urls)
    direct = await _probe_direct()
    _log_probe_result("retry_configured", configured)
    _log_probe_result("retry_direct", direct)
    if configured.rest_ok or direct.rest_ok:
        return settings
    LOG.warning("Binance REST unreachable — re-running autonomous discovery")
    best = await auto_discover_proxies()
    if len(best) < _MIN_WORKING_PROXIES:
        return settings
    _write_proxies_to_config(_config_path or Path(settings.config_path), best)
    net = settings.network.model_copy(
        update={"proxy_url": best[0], "proxy_urls": best[1:], "failover_enabled": True}
    )
    return settings.model_copy(update={"network": net})
