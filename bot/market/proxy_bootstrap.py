"""Autonomous proxy management: discover → validate → connect → rotate.

Research baseline 2026 (20+ projects, stars-sorted):
  SOCKS5 lists  — TheSpeedX/PROXY-List ~5.6k★, proxifly ~5.5k★, monosans/proxy-list,
                  hookzof, ErcinDedeoglu, fyvri, proxygenerator1, vakhov/fresh-proxy-list,
                  Thordata/awesome-free-proxy-list, dinoz0rg/proxy-list, Firmfox/Proxify,
                  VPSLabCloud, iplocate, databay-labs, gfpcom, ClearProxy, Anonym0usWork1221
  HTTP fallback — monosans HTTP, TheSpeedX HTTP, proxifly HTTP, ErcinDedeoglu HTTP,
                  vakhov HTTP, Thordata HTTP, fyvri HTTP
  APIs          — ProxyScrape v4 (1-min freshness, SOCKS5+HTTP)
  Tor           — local daemon on 127.0.0.1:9050 + stem NEWNYM circuit rotation
  ProxyBroker2  — bluet/proxybroker2, asyncio, 50+ sources (optional dep)

On startup ``ensure_network_ready`` is called.  If direct Binance egress
works and no explicit proxy_url is set, auto-discovered pools are cleared
so both REST and WS go direct.  If blocked, the module fetches 20+ public
no-auth lists, validates against the real Binance fstream endpoint with 80
concurrent workers, keeps fastest working proxies, and hot-swaps the live
pool without restarting.

Tor circuit rotation: when a local Tor daemon + stem library are available,
``tor_rotate_circuit()`` sends NEWNYM every refresh cycle for a fresh exit IP.
"""

from __future__ import annotations

import asyncio
import contextlib
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
# ---------------------------------------------------------------------------
# Proxy source lists — 2026 research baseline (stars-sorted)
# ---------------------------------------------------------------------------

_SOCKS5_SOURCES: list[str] = [
    # ① ProxyScrape v4 API — 1-min freshness, ~22k proxies, socks5://ip:port
    (
        "https://api.proxyscrape.com/v4/free-proxy-list/get"
        "?request=display_proxies&proxy_format=protocolipport"
        "&format=text&proxy_type=socks5"
    ),
    # ② TheSpeedX/PROXY-List ~5.6k★ — daily, 9k+ proxies
    "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/socks5.txt",
    # ③ proxifly/free-proxy-list ~5.5k★ — 5-min freshness
    "https://raw.githubusercontent.com/proxifly/free-proxy-list/main/proxies/protocols/socks5/data.txt",
    # ④ monosans/proxy-list — hourly pre-validated + geolocation
    "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/socks5.txt",
    # ⑤ hookzof/socks5_list — auto-updated, Telegram-proxies verified
    "https://raw.githubusercontent.com/hookzof/socks5_list/master/proxy.txt",
    # ⑥ ErcinDedeoglu/proxies — hourly, daily-fresh
    "https://raw.githubusercontent.com/ErcinDedeoglu/proxies/main/proxies/socks5.txt",
    # ⑦ vakhov/fresh-proxy-list — GitHub Pages, daily tested (2026 research)
    "https://vakhov.github.io/fresh-proxy-list/socks5.txt",
    # ⑧ Thordata/awesome-free-proxy-list — GitHub Actions daily auto-verified
    "https://raw.githubusercontent.com/Thordata/awesome-free-proxy-list/main/proxy/socks5.txt",
    # ⑨ dinoz0rg/proxy-list — scraped + checked version (2026 research)
    "https://raw.githubusercontent.com/dinoz0rg/proxy-list/main/checked_proxies/socks5.txt",
    # ⑩ Firmfox/Proxify — SOCKS5 + V2Ray configs, 50+ sources, hourly (2026 research)
    "https://raw.githubusercontent.com/Firmfox/Proxify/main/proxy/socks5.txt",
    # ⑪ fyvri/fresh-proxy-list — hourly, multi-format (JSON/TXT/CSV/XML/YAML)
    "https://raw.githubusercontent.com/fyvri/fresh-proxy-list/main/source/classic/socks5.txt",
    # ⑫ proxygenerator1/ProxyGenerator — deeply verified MostStable tier
    "https://raw.githubusercontent.com/proxygenerator1/ProxyGenerator/main/MostStable/socks5.txt",
    # ⑬ VPSLabCloud — 15-min freshness, elite+anonymous+transparent tiers
    "https://raw.githubusercontent.com/VPSLabCloud/VPSLab-Free-Proxy-List/main/socks5.txt",
    # ⑭ iplocate/free-proxy-list — 30-min freshness
    "https://raw.githubusercontent.com/iplocate/free-proxy-list/main/proxies/socks5.txt",
    # ⑮ databay-labs — 5-min freshness, strict SSL, zero MITM policy
    "https://raw.githubusercontent.com/databay-labs/free-proxy-list/main/proxies/socks5.txt",
    # ⑯ gfpcom — 30-min, includes V2Ray/XRay/Wireguard configs too
    "https://raw.githubusercontent.com/gfpcom/free-proxy-list/main/socks5.txt",
    # ⑰ Anonym0usWork1221/Free-Proxies — community maintained
    "https://raw.githubusercontent.com/Anonym0usWork1221/Free-Proxies/main/proxy_files/socks5_proxies.txt",
    # ⑱ ClearProxy/checked-proxy-list — 5-min, verified against Google/Discord/X
    "https://raw.githubusercontent.com/ClearProxy/checked-proxy-list/main/data/socks5.txt",
    # ⑲ ShiftyTR/Proxy-List — curated community list
    "https://raw.githubusercontent.com/ShiftyTR/Proxy-List/master/socks5.txt",
    # ⑳ openproxy.space — community-verified no-auth API
    "https://openproxy.space/list/socks5",
]

# HTTP proxy sources — fallback when SOCKS5 pool is insufficient
_HTTP_SOURCES: list[str] = [
    # ProxyScrape v4 HTTP — 1-min freshness
    (
        "https://api.proxyscrape.com/v4/free-proxy-list/get"
        "?request=display_proxies&proxy_format=protocolipport"
        "&format=text&proxy_type=http"
    ),
    # monosans HTTP — hourly pre-validated
    "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/http.txt",
    # TheSpeedX HTTP
    "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt",
    # proxifly HTTP — 5-min freshness
    "https://raw.githubusercontent.com/proxifly/free-proxy-list/main/proxies/protocols/http/data.txt",
    # ErcinDedeoglu HTTP — hourly
    "https://raw.githubusercontent.com/ErcinDedeoglu/proxies/main/proxies/http.txt",
    # vakhov HTTP — daily tested (GitHub Pages, 2026 research)
    "https://vakhov.github.io/fresh-proxy-list/http.txt",
    # Thordata HTTP — GitHub Actions daily
    "https://raw.githubusercontent.com/Thordata/awesome-free-proxy-list/main/proxy/http.txt",
    # fyvri HTTP — hourly
    "https://raw.githubusercontent.com/fyvri/fresh-proxy-list/main/source/classic/http.txt",
]

# Keep legacy alias so external callers (scripts/) still work
_PROXY_SOURCES = _SOCKS5_SOURCES

# Binance futures REST ping — lightweight, returns {} on success
_BINANCE_PING_URL = "https://fapi.binance.com/fapi/v1/ping"
_BINANCE_WS_HANDSHAKE_URL = "wss://fstream.binance.com/ws"

_VALIDATE_CONCURRENCY = 80       # simultaneous proxy checks
_VALIDATE_TIMEOUT_S = 10.0       # per-proxy validation timeout (SOCKS5+SSL needs ~6-8s)
_FETCH_TIMEOUT_S = 15.0          # per-source HTTP fetch timeout
_MAX_CANDIDATES = 2000           # cap before validation (memory guard)
_MIN_WORKING_PROXIES = 2         # minimum to accept discovery result
_MAX_POOL_SIZE = 25              # proxies kept in the live pool
_REST_SYMBOL_THRESHOLD = 100
_WS_PROBE_TIMEOUT_SECONDS = 15.0
_POOL_REFRESH_INTERVAL_S = 900.0   # background refresh every 15 min (was 30)


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

async def _detect_local_tor() -> str | None:
    """Return socks5://127.0.0.1:9050 if a local Tor SOCKS daemon is reachable."""
    try:
        async with asyncio.timeout(2.0):
            _r, writer = await asyncio.open_connection("127.0.0.1", 9050)
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
        LOG.info("local Tor daemon detected on 127.0.0.1:9050 — adding to pool")
        return "socks5://127.0.0.1:9050"
    except Exception:
        return None


async def tor_rotate_circuit(*, control_port: int = 9051, timeout: float = 15.0) -> bool:
    """
    Signal Tor to build a fresh circuit (new exit IP) via NEWNYM.

    Requires:
    - Tor daemon running with ControlPort enabled (torrc: ``ControlPort 9051``)
    - ``stem`` library installed (``pip install stem``)
    - CookieAuthentication or no auth (default for most local installs)

    Returns True on success, False if stem is unavailable or control port unreachable.
    Used by ``run_proxy_refresh_loop`` to rotate the Tor exit IP every refresh cycle.
    """
    try:
        import stem  # type: ignore[import-untyped]
        import stem.control  # type: ignore[import-untyped]
    except ImportError:
        return False

    loop = asyncio.get_event_loop()

    def _do_rotate() -> bool:
        try:
            with stem.control.Controller.from_port(port=control_port) as ctrl:
                ctrl.authenticate()
                wait = ctrl.get_newnym_wait()
                if wait > 0:
                    LOG.debug("Tor NEWNYM cooldown %.1fs — waiting", wait)
                    time.sleep(min(wait, timeout * 0.8))
                ctrl.signal(stem.Signal.NEWNYM)
                return True
        except Exception as exc:
            LOG.debug("Tor circuit rotation failed: %s", exc)
            return False

    try:
        async with asyncio.timeout(timeout):
            result: bool = await loop.run_in_executor(None, _do_rotate)
            if result:
                LOG.info("Tor circuit rotated via NEWNYM — fresh exit IP active")
            return result
    except TimeoutError:
        LOG.debug("Tor circuit rotation timed out after %.1fs", timeout)
        return False


async def _fetch_source(
    session: aiohttp.ClientSession,
    url: str,
    *,
    protocol: str = "socks5",
) -> list[str]:
    """Fetch one public proxy list; return normalized ``<protocol>://ip:port`` lines."""
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

    scheme = protocol.lower()
    out: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith(f"{scheme}://"):
            out.append(line)
        elif line.startswith("socks5://") and scheme == "socks5":
            out.append(line)
        elif line.startswith("http://") and scheme == "http":
            out.append(line)
        elif "://" not in line and ":" in line:
            # bare ip:port — prefix with requested protocol
            out.append(f"{scheme}://{line}")
        # skip mismatched protocols
    return out


async def _validate_proxy_binance(
    proxy_url: str,
    sem: asyncio.Semaphore,
    timeout: float = _VALIDATE_TIMEOUT_S,
) -> tuple[str, float] | None:
    """
    Validate *proxy_url* against the real Binance fstream ping endpoint.
    Supports SOCKS5 (via aiohttp_socks) and HTTP CONNECT proxies.
    Returns ``(proxy_url, latency_ms)`` on success, ``None`` on any failure.
    """
    is_socks = proxy_url.startswith("socks")
    if is_socks:
        try:
            from aiohttp_socks import ProxyConnector  # type: ignore[import-untyped]
        except ImportError:
            return None

    async with sem:
        connector: aiohttp.BaseConnector | None = None
        try:
            t0 = time.monotonic()
            async with asyncio.timeout(timeout):
                if is_socks:
                    from aiohttp_socks import ProxyConnector  # type: ignore[import-untyped]
                    connector = ProxyConnector.from_url(proxy_url, rdns=True)
                    async with aiohttp.ClientSession(connector=connector) as sess:
                        async with sess.get(_BINANCE_PING_URL, ssl=True) as resp:
                            if resp.status == 200:
                                return proxy_url, (time.monotonic() - t0) * 1000.0
                else:
                    # HTTP CONNECT proxy — pass via per-request param
                    connector = aiohttp.TCPConnector()
                    async with aiohttp.ClientSession(connector=connector) as sess:
                        async with sess.get(
                            _BINANCE_PING_URL, proxy=proxy_url, ssl=True
                        ) as resp:
                            if resp.status == 200:
                                return proxy_url, (time.monotonic() - t0) * 1000.0
        except Exception:
            pass
        finally:
            if connector is not None and not connector.closed:
                connector.close()
    return None


async def _gather_candidates(
    sources: list[str],
    *,
    protocol: str = "socks5",
) -> list[str]:
    """Fetch all sources in parallel and return deduplicated proxy URLs."""
    connector_limit = max(len(sources), 20)
    async with aiohttp.ClientSession(
        connector=aiohttp.TCPConnector(limit=connector_limit, ssl=False),
        headers={"User-Agent": "python-aiohttp/3"},
        timeout=aiohttp.ClientTimeout(total=_FETCH_TIMEOUT_S + 5),
    ) as session:
        fetch_results = await asyncio.gather(
            *[_fetch_source(session, url, protocol=protocol) for url in sources],
            return_exceptions=True,
        )
    seen: set[str] = set()
    out: list[str] = []
    for r in fetch_results:
        if isinstance(r, list):
            for url in r:
                if url not in seen:
                    seen.add(url)
                    out.append(url)
    return out


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
    LOG.info(
        "proxy auto-discovery: %d SOCKS5 sources + %d HTTP fallback + Tor detection",
        len(_SOCKS5_SOURCES),
        len(_HTTP_SOURCES),
    )

    # 1. Detect local Tor in parallel with source fetching
    tor_task: asyncio.Task[str | None] = asyncio.create_task(_detect_local_tor())

    # 2. Fetch SOCKS5 candidates from all sources
    candidates = await _gather_candidates(_SOCKS5_SOURCES, protocol="socks5")
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
        http_candidates = await _gather_candidates(_HTTP_SOURCES, protocol="http")
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

        # Rotate Tor circuit first (new exit IP before validating new pool)
        tor_rotated = await tor_rotate_circuit()

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
            "proxy refresh complete | active=%s pool_size=%d tor_rotated=%s",
            best[0], len(best), tor_rotated,
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
