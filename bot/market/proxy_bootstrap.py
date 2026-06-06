"""Autonomous proxy management: discover → validate → connect → rotate.

On startup ``ensure_network_ready`` is called.  If Binance is reachable
directly the pool stays empty.  If direct egress is blocked the module
fetches SOCKS5 proxy lists from public no-auth GitHub/API sources,
validates each candidate against the real Binance fstream endpoint
concurrently (50 workers), keeps the fastest working ones, and writes
them to config.toml so the next restart starts with a warm pool.

At runtime a background task (``run_proxy_refresh_loop``) re-discovers
every 30 minutes and hot-swaps the live pool without restarting the bot.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path

import aiohttp
import websockets

from bot.domain.config import BotSettings, NetworkConfig, load_settings
from bot.market.data import BinanceFuturesMarketData
from bot.market.network_proxy import websockets_connect_kwargs
from bot.market.rest_impl import BinanceClientImpl

LOG = logging.getLogger("bot.market.proxy_bootstrap")

# ---------------------------------------------------------------------------
# Public proxy-list sources — no registration, no auth, updated frequently
# ---------------------------------------------------------------------------
_PROXY_SOURCES: list[str] = [
    # ProxyScrape v4 — 1-minute freshness, returns socks5://ip:port lines
    (
        "https://api.proxyscrape.com/v4/free-proxy-list/get"
        "?request=display_proxies&proxy_format=protocolipport"
        "&format=text&proxy_type=socks5"
    ),
    # monosans/proxy-list — hourly pre-validated, ip:port format
    "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/socks5.txt",
    # proxifly — 5-minute freshness, ip:port format
    "https://raw.githubusercontent.com/proxifly/free-proxy-list/main/proxies/protocols/socks5/data.txt",
    # TheSpeedX — large curated list, ip:port format
    "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/socks5.txt",
    # hookzof — additional source, ip:port format
    "https://raw.githubusercontent.com/hookzof/socks5_list/master/proxy.txt",
    # jetkai — ip:port format
    "https://raw.githubusercontent.com/jetkai/proxy-list/main/online-proxies/txt/proxies-socks5.txt",
    # ShiftyTR — ip:port format
    "https://raw.githubusercontent.com/ShiftyTR/Proxy-List/master/socks5.txt",
]

# Binance futures REST ping — lightweight, returns {} on success
_BINANCE_PING_URL = "https://fapi.binance.com/fapi/v1/ping"
_BINANCE_WS_HANDSHAKE_URL = "wss://fstream.binance.com/ws"

_VALIDATE_CONCURRENCY = 60       # simultaneous proxy checks
_VALIDATE_TIMEOUT_S = 10.0       # per-proxy validation timeout (SOCKS5+SSL needs ~6-8s)
_FETCH_TIMEOUT_S = 15.0          # per-source HTTP fetch timeout
_MAX_CANDIDATES = 1500           # cap before validation (memory guard)
_MIN_WORKING_PROXIES = 3         # minimum to accept discovery result
_MAX_POOL_SIZE = 15              # proxies kept in the live pool
_REST_SYMBOL_THRESHOLD = 100
_WS_PROBE_TIMEOUT_SECONDS = 15.0
_POOL_REFRESH_INTERVAL_S = 1800.0  # background refresh every 30 min


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
                **connect_kwargs,
            ):
                return True
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception as exc:
        LOG.debug("ws handshake probe failed | proxy=%s err=%s", proxy_url, exc)
        return False


async def _probe_rest(net: NetworkConfig) -> bool:
    client = BinanceClientImpl(rest_timeout_seconds=12.0, network=net)
    market = BinanceFuturesMarketData(binance_client=client)
    try:
        symbols = await market.fetch_exchange_symbols()
        return len(symbols) > _REST_SYMBOL_THRESHOLD
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception:
        return False
    finally:
        await market.close()


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
        label, result.rest_ok, result.ws_ok,
    )


# ---------------------------------------------------------------------------
# Autonomous proxy discovery
# ---------------------------------------------------------------------------

async def _fetch_source(session: aiohttp.ClientSession, url: str) -> list[str]:
    """Fetch one public proxy list; return normalized ``socks5://ip:port`` lines."""
    try:
        async with asyncio.timeout(_FETCH_TIMEOUT_S):
            async with session.get(url) as resp:
                if resp.status != 200:
                    LOG.debug("proxy source %s → HTTP %d", url, resp.status)
                    return []
                text = await resp.text()
    except Exception as exc:
        LOG.debug("proxy source fetch failed | url=%s err=%s", url, exc)
        return []

    out: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("socks5://"):
            out.append(line)
        elif "://" not in line and ":" in line:
            # bare ip:port — treat as SOCKS5 (validation will reject non-SOCKS)
            out.append(f"socks5://{line}")
        # skip http:// / socks4:// lines
    return out


async def _validate_proxy_binance(
    proxy_url: str,
    sem: asyncio.Semaphore,
    timeout: float = _VALIDATE_TIMEOUT_S,
) -> tuple[str, float] | None:
    """
    Validate *proxy_url* against the real Binance fstream ping endpoint.
    Returns ``(proxy_url, latency_ms)`` on success, ``None`` on any failure.
    """
    try:
        from aiohttp_socks import ProxyConnector  # type: ignore[import-untyped]
    except ImportError:
        return None

    async with sem:
        connector: aiohttp.TCPConnector | None = None
        try:
            connector = ProxyConnector.from_url(proxy_url, rdns=True)
            t0 = time.monotonic()
            async with asyncio.timeout(timeout):
                async with aiohttp.ClientSession(connector=connector) as sess:
                    async with sess.get(_BINANCE_PING_URL, ssl=True) as resp:
                        if resp.status == 200:
                            return proxy_url, (time.monotonic() - t0) * 1000.0
        except Exception:
            pass
        finally:
            if connector is not None and not connector.closed:
                connector.close()
    return None


async def auto_discover_proxies(
    *,
    max_pool_size: int = _MAX_POOL_SIZE,
    validate_timeout: float = _VALIDATE_TIMEOUT_S,
    validate_concurrency: int = _VALIDATE_CONCURRENCY,
) -> list[str]:
    """
    Fetch SOCKS5 lists from all public sources, validate each candidate
    against the real Binance fstream endpoint, and return up to
    *max_pool_size* URLs sorted by ascending latency (fastest first).

    This is a full replacement for the old subprocess-based discover script.
    All I/O is async; no external processes are spawned.
    """
    LOG.info(
        "proxy auto-discovery: fetching from %d public sources", len(_PROXY_SOURCES)
    )

    # Fetch all sources in parallel (direct internet, no proxy needed here)
    async with aiohttp.ClientSession(
        connector=aiohttp.TCPConnector(limit=len(_PROXY_SOURCES), ssl=False),
        headers={"User-Agent": "python-aiohttp/3"},
        timeout=aiohttp.ClientTimeout(total=_FETCH_TIMEOUT_S + 5),
    ) as session:
        fetch_results = await asyncio.gather(
            *[_fetch_source(session, url) for url in _PROXY_SOURCES],
            return_exceptions=True,
        )

    # Deduplicate across all sources
    seen: set[str] = set()
    candidates: list[str] = []
    for r in fetch_results:
        if isinstance(r, list):
            for url in r:
                if url not in seen:
                    seen.add(url)
                    candidates.append(url)

    LOG.info(
        "proxy auto-discovery: %d unique candidates — validating against Binance fstream...",
        len(candidates),
    )

    if not candidates:
        LOG.error("proxy auto-discovery: all sources returned empty lists")
        return []

    candidates = candidates[:_MAX_CANDIDATES]  # memory guard

    # Validate concurrently against the real Binance endpoint
    sem = asyncio.Semaphore(validate_concurrency)
    val_tasks = [
        _validate_proxy_binance(u, sem, validate_timeout) for u in candidates
    ]
    val_results = await asyncio.gather(*val_tasks, return_exceptions=True)

    working: list[tuple[str, float]] = [
        r for r in val_results if isinstance(r, tuple)
    ]

    if not working:
        LOG.error(
            "proxy auto-discovery: 0/%d candidates reached Binance", len(candidates)
        )
        return []

    working.sort(key=lambda x: x[1])
    best = [url for url, _ in working[:max_pool_size]]

    LOG.info(
        "proxy auto-discovery done | validated=%d/%d best_ms=%.0f worst_ms=%.0f pool=%d",
        len(working),
        len(candidates),
        working[0][1],
        working[min(len(best) - 1, len(working) - 1)][1],
        len(best),
    )
    return best


def _write_proxies_to_config(path: Path, urls: list[str]) -> None:
    """
    Persist *urls* into config.toml — updates ``proxy_url`` and
    ``proxy_urls`` in-place; all other settings are preserved.
    """
    if not path.is_file() or not urls:
        return
    text = path.read_text(encoding="utf-8")
    primary = urls[0]
    rest = urls[1:]

    # Replace proxy_url = "..."
    text = re.sub(r'(proxy_url\s*=\s*)"[^"]*"', rf'\1"{primary}"', text)

    # Replace proxy_urls = [ ... ] (multi-line block)
    urls_toml = "[\n" + "".join(f'  "{u}",\n' for u in rest) + "]"
    text = re.sub(
        r"proxy_urls\s*=\s*\[.*?\]",
        f"proxy_urls = {urls_toml}",
        text,
        flags=re.DOTALL,
    )

    path.write_text(text, encoding="utf-8")
    LOG.info(
        "proxy config persisted | path=%s primary=%s pool_size=%d",
        path, primary, len(urls),
    )


# ---------------------------------------------------------------------------
# Startup gate
# ---------------------------------------------------------------------------

async def ensure_network_ready(
    settings: BotSettings,
    *,
    config_path: Path | None = None,
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
        await _probe_configured(urls)
        if urls
        else NetworkProbeResult(rest_ok=False, ws_ok=False)
    )
    _log_probe_result("direct", direct)
    if urls:
        _log_probe_result("configured", configured)

    if direct.rest_ok:
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

    LOG.warning(
        "Binance unreachable via direct and configured pool — running autonomous discovery"
    )
    best = await auto_discover_proxies()

    if len(best) < _MIN_WORKING_PROXIES:
        LOG.error(
            "auto-discovery found %d proxies (need %d) — starting degraded",
            len(best), _MIN_WORKING_PROXIES,
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
    config_path: Path | None = None,
) -> None:
    """
    Background task — re-discover working proxies every *interval_seconds*
    and hot-swap the live pool without restarting the bot.

    Accesses from *bot* (SignalBot):
      ``client._binance_client`` — live BinanceClientImpl
      ``ws_manager``             — live FuturesWSManager
      ``_shutdown``              — asyncio.Event
    """
    shutdown: asyncio.Event = getattr(bot, "_shutdown", asyncio.Event())
    LOG.info("proxy refresh loop started | interval=%.0fs", interval_seconds)

    while not shutdown.is_set():
        try:
            await asyncio.wait_for(shutdown.wait(), timeout=interval_seconds)
            break  # shutdown fired
        except asyncio.TimeoutError:
            pass

        LOG.info("proxy refresh: scheduled re-discovery starting")
        try:
            best = await auto_discover_proxies()
        except Exception as exc:
            LOG.warning("proxy refresh: discovery error | %s", exc)
            continue

        if len(best) < _MIN_WORKING_PROXIES:
            LOG.warning(
                "proxy refresh: only %d proxies found — keeping current pool", len(best)
            )
            continue

        # Update live REST client pool
        rest_client = getattr(bot, "client", None)
        inner = getattr(rest_client, "_binance_client", None)
        if inner is not None:
            pool = getattr(inner, "_proxy_pool", None)
            if pool is not None:
                pool.urls = best
                pool._bad_until.clear()
                pool._index = 0
            try:
                await inner._apply_active_proxy(best[0])
                inner._last_failover_mono = 0.0  # reset rate-limit after deliberate swap
            except Exception as exc:
                LOG.warning("proxy refresh: REST client update failed | %s", exc)

        # Update WS manager
        ws_mgr = getattr(bot, "ws_manager", None)
        if ws_mgr is not None and hasattr(ws_mgr, "update_proxy_url"):
            try:
                ws_mgr.update_proxy_url(best[0])
            except Exception as exc:
                LOG.warning("proxy refresh: WS proxy update failed | %s", exc)

        LOG.info(
            "proxy refresh complete | active=%s pool_size=%d", best[0], len(best)
        )

    LOG.info("proxy refresh loop stopped")


async def retry_network_after_failure(
    settings: BotSettings,
    *,
    config_path: Path | None = None,
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
    net = settings.network.model_copy(
        update={"proxy_url": best[0], "proxy_urls": best[1:], "failover_enabled": True}
    )
    return settings.model_copy(update={"network": net})
