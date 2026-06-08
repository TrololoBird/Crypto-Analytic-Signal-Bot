"""Proxy source list constants and raw HTTP fetchers.

Extracted from proxy_bootstrap to reduce module size (795→~400 LOC).
Owns: source URL lists, timeout constants, _fetch_source, _gather_candidates.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path

import aiohttp

from bot.runtime.errors import defensive_exc_types

LOG = logging.getLogger("bot.market._proxy_sources")

# ---------------------------------------------------------------------------
# Proxy source lists — 2026 research baseline (stars-sorted)
# ---------------------------------------------------------------------------

_SOCKS5_SOURCES: list[str] = [
    # ProxyScrape v4 API — 1-min freshness, ~22k proxies, socks5://ip:port
    (
        "https://api.proxyscrape.com/v4/free-proxy-list/get"
        "?request=display_proxies&proxy_format=protocolipport"
        "&format=text&proxy_type=socks5"
    ),
    # TheSpeedX/PROXY-List ~5.6k★ — daily, 9k+ proxies
    "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/socks5.txt",
    # proxifly/free-proxy-list ~5.5k★ — 5-min freshness
    "https://raw.githubusercontent.com/proxifly/free-proxy-list/main/proxies/protocols/socks5/data.txt",
    # monosans/proxy-list — hourly pre-validated + geolocation
    "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/socks5.txt",
    # hookzof/socks5_list — auto-updated, Telegram-proxies verified
    "https://raw.githubusercontent.com/hookzof/socks5_list/master/proxy.txt",
    # ErcinDedeoglu/proxies — hourly, daily-fresh
    "https://raw.githubusercontent.com/ErcinDedeoglu/proxies/main/proxies/socks5.txt",
    # vakhov/fresh-proxy-list — GitHub Pages, daily tested
    "https://vakhov.github.io/fresh-proxy-list/socks5.txt",
    # Thordata/awesome-free-proxy-list — GitHub Actions daily auto-verified
    "https://raw.githubusercontent.com/Thordata/awesome-free-proxy-list/main/proxy/socks5.txt",
    # dinoz0rg/proxy-list — scraped + checked version
    "https://raw.githubusercontent.com/dinoz0rg/proxy-list/main/checked_proxies/socks5.txt",
    # Firmfox/Proxify — SOCKS5 + V2Ray configs, 50+ sources, hourly
    "https://raw.githubusercontent.com/Firmfox/Proxify/main/proxy/socks5.txt",
    # fyvri/fresh-proxy-list — hourly, multi-format (JSON/TXT/CSV/XML/YAML)
    "https://raw.githubusercontent.com/fyvri/fresh-proxy-list/main/source/classic/socks5.txt",
    # proxygenerator1/ProxyGenerator — deeply verified MostStable tier
    "https://raw.githubusercontent.com/proxygenerator1/ProxyGenerator/main/MostStable/socks5.txt",
    # VPSLabCloud — 15-min freshness, elite+anonymous+transparent tiers
    "https://raw.githubusercontent.com/VPSLabCloud/VPSLab-Free-Proxy-List/main/socks5.txt",
    # iplocate/free-proxy-list — 30-min freshness
    "https://raw.githubusercontent.com/iplocate/free-proxy-list/main/proxies/socks5.txt",
    # databay-labs — 5-min freshness, strict SSL, zero MITM policy
    "https://raw.githubusercontent.com/databay-labs/free-proxy-list/main/proxies/socks5.txt",
    # gfpcom — 30-min, includes V2Ray/XRay/Wireguard configs too
    "https://raw.githubusercontent.com/gfpcom/free-proxy-list/main/socks5.txt",
    # Anonym0usWork1221/Free-Proxies — community maintained
    "https://raw.githubusercontent.com/Anonym0usWork1221/Free-Proxies/main/proxy_files/socks5_proxies.txt",
    # ClearProxy/checked-proxy-list — 5-min, verified against Google/Discord/X
    "https://raw.githubusercontent.com/ClearProxy/checked-proxy-list/main/data/socks5.txt",
    # ShiftyTR/Proxy-List — curated community list
    "https://raw.githubusercontent.com/ShiftyTR/Proxy-List/master/socks5.txt",
    # openproxy.space — community-verified no-auth API
    "https://openproxy.space/list/socks5",
]

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
    # vakhov HTTP — daily tested (GitHub Pages)
    "https://vakhov.github.io/fresh-proxy-list/http.txt",
    # Thordata HTTP — GitHub Actions daily
    "https://raw.githubusercontent.com/Thordata/awesome-free-proxy-list/main/proxy/http.txt",
    # fyvri HTTP — hourly
    "https://raw.githubusercontent.com/fyvri/fresh-proxy-list/main/source/classic/http.txt",
]

# Legacy alias so external callers (scripts/) still work
_PROXY_SOURCES = _SOCKS5_SOURCES

# ---------------------------------------------------------------------------
# Validation / fetch tuning
# ---------------------------------------------------------------------------

_BINANCE_PING_URL = "https://fapi.binance.com/fapi/v1/ping"
_BINANCE_WS_HANDSHAKE_URL = "wss://fstream.binance.com/ws"

_VALIDATE_CONCURRENCY = 80
_VALIDATE_TIMEOUT_S = 10.0
_FETCH_TIMEOUT_S = 15.0
_MAX_CANDIDATES = 2000
_MIN_WORKING_PROXIES = 2
_MAX_POOL_SIZE = 25
_REST_SYMBOL_THRESHOLD = 100
_WS_PROBE_TIMEOUT_SECONDS = 15.0
_POOL_REFRESH_INTERVAL_S = 900.0


# ---------------------------------------------------------------------------
# Source fetchers
# ---------------------------------------------------------------------------


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
                    LOG.debug("proxy source %s -> HTTP %d", url, resp.status)
                    return []
                text = await resp.text()
    except defensive_exc_types(aiohttp.ClientError) as exc:
        LOG.debug("proxy source fetch failed | url=%s err=%s", url, exc)
        return []

    scheme = protocol.lower()
    out: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if (
            line.startswith(f"{scheme}://")
            or (line.startswith("socks5://") and scheme == "socks5")
            or (line.startswith("http://") and scheme == "http")
        ):
            out.append(line)
        elif "://" not in line and ":" in line:
            # bare ip:port — prefix with requested protocol
            out.append(f"{scheme}://{line}")
    return out


def _load_custom_sources() -> list[str]:
    """Load custom proxy source URLs from external config.

    Sources (first wins):
      1. ``config/proxy_sources.json`` — JSON array of source URLs
      2. ``$PROXY_CUSTOM_SOURCES`` — JSON array as env var

    These are merged transparently into the SOCKS5 and HTTP fallback
    source lists at bootstrap time (no code changes needed to add sources).
    """
    urls: list[str] = []
    # 1. File-based custom sources
    custom_path = Path("config/proxy_sources.json")
    if custom_path.is_file():
        try:
            data = json.loads(custom_path.read_text(encoding="utf-8"))
            if isinstance(data, list):
                urls.extend(str(u) for u in data)
                LOG.info("loaded %d custom proxy sources from %s", len(data), custom_path)
        except (json.JSONDecodeError, OSError) as exc:
            LOG.warning("failed to load custom proxy sources from %s: %s", custom_path, exc)
    # 2. Environment variable
    env_raw = os.environ.get("PROXY_CUSTOM_SOURCES", "")
    if env_raw:
        try:
            env_data = json.loads(env_raw)
            if isinstance(env_data, list):
                urls.extend(str(u) for u in env_data)
                LOG.info("loaded %d custom proxy sources from $PROXY_CUSTOM_SOURCES", len(env_data))
        except (json.JSONDecodeError, TypeError) as exc:
            LOG.warning("failed to parse $PROXY_CUSTOM_SOURCES: %s", exc)
    return urls


async def _gather_candidates(
    sources: list[str],
    *,
    protocol: str = "socks5",
) -> list[str]:
    """Fetch all sources in parallel and return deduplicated proxy URLs."""
    connector_limit = max(len(sources), 20)
    async with aiohttp.ClientSession(
        connector=aiohttp.TCPConnector(limit=connector_limit, ssl=True),
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
