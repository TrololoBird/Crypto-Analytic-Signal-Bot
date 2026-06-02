"""Resolve and apply egress proxy settings for Binance REST and WebSocket."""

from __future__ import annotations

import logging
import os
from urllib.parse import urlparse, urlunparse

import aiohttp

try:
    from aiohttp_socks import ProxyConnector
except ImportError:
    ProxyConnector = None  # type: ignore[misc, assignment]

try:
    import python_socks
except ImportError:
    python_socks = None  # type: ignore[misc, assignment]

LOG = logging.getLogger("bot.market.network_proxy")

_PROXY_ENV_KEYS: tuple[str, ...] = (
    "BINANCE_PROXY_URL",
    "HTTPS_PROXY",
    "https_proxy",
    "ALL_PROXY",
    "all_proxy",
    "WSS_PROXY",
    "wss_proxy",
)


def resolve_proxy_url(*, config_url: str | None = None, trust_env: bool = True) -> str | None:
    """Return proxy URL from config (first) or standard environment variables."""
    configured = str(config_url or "").strip()
    if configured:
        return configured
    if not trust_env:
        return None
    for key in _PROXY_ENV_KEYS:
        value = os.environ.get(key, "").strip()
        if value:
            return value
    return None


def proxy_scheme(url: str) -> str:
    return urlparse(url).scheme.lower()


def is_socks_proxy(url: str) -> bool:
    return proxy_scheme(url).startswith("socks")


def normalize_proxy_url(url: str) -> str:
    """Map ``socks5h`` to ``socks5`` for libraries that only accept ``socks5`` (DNS via rdns)."""
    parsed = urlparse(url)
    if parsed.scheme.lower() == "socks5h":
        return urlunparse(parsed._replace(scheme="socks5"))
    return url


def mask_proxy_url(url: str) -> str:
    parsed = urlparse(url)
    if not parsed.hostname:
        return url
    host = parsed.hostname
    port = f":{parsed.port}" if parsed.port else ""
    scheme = parsed.scheme or "http"
    if parsed.username:
        return f"{scheme}://***:***@{host}{port}"
    return f"{scheme}://{host}{port}"


def apply_proxy_env(url: str | None) -> None:
    """Mirror resolved proxy into HTTPS_PROXY / WSS_PROXY for libraries that read env only."""
    if not url:
        return
    os.environ.setdefault("HTTPS_PROXY", url)
    os.environ.setdefault("https_proxy", url)
    if is_socks_proxy(url):
        os.environ.setdefault("WSS_PROXY", url)
        os.environ.setdefault("wss_proxy", url)
    else:
        os.environ.setdefault("WSS_PROXY", url)
        os.environ.setdefault("wss_proxy", url)


def create_aiohttp_session(
    *,
    proxy_url: str | None,
    trust_env: bool,
    timeout: aiohttp.ClientTimeout,
    connector_limit: int,
) -> aiohttp.ClientSession:
    if proxy_url and is_socks_proxy(proxy_url):
        if ProxyConnector is None:  # pragma: no cover - dependency guard
            msg = "SOCKS proxy requires aiohttp-socks (pip install aiohttp-socks)"
            raise RuntimeError(msg)
        socks_connector = ProxyConnector.from_url(
            normalize_proxy_url(proxy_url),
            limit=connector_limit,
            rdns=True,
        )
        LOG.info("rest proxy enabled | mode=socks url=%s", mask_proxy_url(proxy_url))
        return aiohttp.ClientSession(timeout=timeout, connector=socks_connector, trust_env=False)

    connector = aiohttp.TCPConnector(
        limit=connector_limit,
        resolver=aiohttp.ThreadedResolver(),
    )
    use_env = trust_env and not proxy_url
    if proxy_url:
        LOG.info("rest proxy enabled | mode=http url=%s", mask_proxy_url(proxy_url))
    elif use_env:
        LOG.info("rest proxy enabled | mode=trust_env")
    session = aiohttp.ClientSession(
        timeout=timeout,
        connector=connector,
        trust_env=use_env,
    )
    if proxy_url:
        session._binance_explicit_proxy = proxy_url
    return session


def aiohttp_request_proxy(session: aiohttp.ClientSession, proxy_url: str | None) -> str | None:
    """Proxy URL for per-request override when not using SOCKS connector."""
    if proxy_url and not is_socks_proxy(proxy_url):
        return proxy_url
    explicit = getattr(session, "_binance_explicit_proxy", None)
    if isinstance(explicit, str) and explicit:
        return explicit
    return None


def websockets_connect_kwargs(
    *,
    proxy_url: str | None,
    trust_env: bool,
) -> dict[str, str | None]:
    """Keyword arguments for ``websockets.connect`` proxy routing."""
    if proxy_url:
        if is_socks_proxy(proxy_url):
            _ensure_python_socks()
        LOG.info("ws proxy enabled | url=%s", mask_proxy_url(proxy_url))
        return {"proxy": normalize_proxy_url(proxy_url)}
    if trust_env:
        return {}
    return {"proxy": None}


def _ensure_python_socks() -> None:
    if python_socks is None:  # pragma: no cover
        msg = (
            "SOCKS WebSocket proxy requires python-socks[asyncio] "
            "(pip install 'python-socks[asyncio]')"
        )
        raise RuntimeError(msg)
