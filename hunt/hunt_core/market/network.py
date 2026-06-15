"""Egress proxy pool and discovery for hunt market plane."""

from __future__ import annotations

import asyncio
import logging
import os
import random
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, cast
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

LOG = logging.getLogger("hunt_core.market.network")

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


def create_aiohttp_session(
    *,
    proxy_url: str | None,
    trust_env: bool,
    timeout: aiohttp.ClientTimeout,
    connector_limit: int,
) -> aiohttp.ClientSession:
    if proxy_url and is_socks_proxy(proxy_url):
        if ProxyConnector is None:  # pragma: no cover
            msg = "SOCKS proxy requires aiohttp-socks (pip install aiohttp-socks)"
            raise RuntimeError(msg)
        socks_connector = ProxyConnector.from_url(
            normalize_proxy_url(proxy_url),
            limit=connector_limit,
            rdns=True,
        )
        LOG.info("hunt rest proxy | mode=socks url=%s", mask_proxy_url(proxy_url))
        return aiohttp.ClientSession(timeout=timeout, connector=socks_connector, trust_env=False)

    connector = aiohttp.TCPConnector(
        limit=connector_limit,
        resolver=aiohttp.ThreadedResolver(),
    )
    use_env = trust_env and not proxy_url
    if proxy_url:
        LOG.info("hunt rest proxy | mode=http url=%s", mask_proxy_url(proxy_url))
    session = aiohttp.ClientSession(
        timeout=timeout,
        connector=connector,
        trust_env=use_env,
    )
    if proxy_url:
        session._binance_explicit_proxy = proxy_url
    return session


async def close_aiohttp_session(session: aiohttp.ClientSession | None) -> None:
    if session is None or session.closed:
        return
    await session.close()


def aiohttp_request_proxy(session: aiohttp.ClientSession, proxy_url: str | None) -> str | None:
    if proxy_url and not is_socks_proxy(proxy_url):
        return proxy_url
    explicit = getattr(session, "_binance_explicit_proxy", None)
    if isinstance(explicit, str) and explicit:
        return explicit
    return None

if TYPE_CHECKING:
    from collections.abc import Callable


LOG = logging.getLogger("hunt_core.market.network")

_CIRCUIT_BREAKER_MAX_FAILURES = 5
_MAX_BACKOFF_SECONDS = 3600.0
_MIN_POOL_SIZE = 2


@dataclass
class BanDetectionPolicy:
    ban_status_codes: frozenset[int] = frozenset({418, 403, 429})
    ban_on_timeout: bool = True
    ban_on_proxy_error: bool = True

    def is_response_banned(self, status: int) -> bool:
        return status in self.ban_status_codes

    def is_exception_banned(self, exc: BaseException) -> bool:
        if not (self.ban_on_proxy_error or self.ban_on_timeout):
            return False
        if self.ban_on_proxy_error and is_proxy_transport_error(exc):
            return True
        if self.ban_on_timeout:
            if isinstance(exc, TimeoutError):
                return True
            if exc.__class__.__name__ == "TimeoutError":
                return True
        return False


@dataclass
class ProxyPool:
    urls: list[str]
    cooldown_seconds: float = 300.0
    _index: int = 0
    _bad_until: dict[str, float] = field(default_factory=dict)
    _success_count: dict[str, int] = field(default_factory=dict)
    _failure_count: dict[str, int] = field(default_factory=dict)
    _success_streak: dict[str, int] = field(default_factory=dict)
    _last_latencies: dict[str, list[float]] = field(default_factory=dict)
    _rolling_window: int = 10
    _failover_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    @classmethod
    def from_urls(
        cls,
        urls: list[str],
        *,
        cooldown_seconds: float = 300.0,
    ) -> ProxyPool | None:
        cleaned = _dedupe_urls(urls)
        if not cleaned:
            return None
        return cls(cleaned, cooldown_seconds=max(30.0, float(cooldown_seconds or 120.0)))

    def has_alternatives(self) -> bool:
        return len(self.urls) > 1

    def current(self) -> str:
        return self.urls[self._index % len(self.urls)]

    def _is_available(self, url: str) -> bool:
        return time.monotonic() >= self._bad_until.get(url, 0.0)

    def _next_available_index(self, _start: int) -> int | None:
        available = [
            (i, self.urls[i]) for i in range(len(self.urls)) if self._is_available(self.urls[i])
        ]
        if not available:
            return None
        if len(available) == 1:
            return available[0][0]
        weights = [self._health_score(url) for _, url in available]
        total = sum(weights)
        if total <= 0:
            return available[0][0]
        chosen = random.choices(range(len(available)), weights=weights, k=1)[0]
        return available[chosen][0]

    def _backoff_duration(self, failures: int) -> float:
        base = min(_MAX_BACKOFF_SECONDS, self.cooldown_seconds * (2 ** (failures - 1)))
        return base + random.uniform(0, base * 0.1)  # type: ignore[no-any-return]

    def mark_failed(self, url: str, reason: str) -> str | None:
        if url in self.urls:
            self._failure_count[url] = self._failure_count.get(url, 0) + 1
            self._success_streak[url] = 0
            backoff = self._backoff_duration(self._failure_count[url])
            self._bad_until[url] = time.monotonic() + backoff
            LOG.warning(
                "hunt proxy bad | url=%s backoff_s=%.0f failures=%d reason=%s",
                mask_proxy_url(url),
                backoff,
                self._failure_count[url],
                reason[:120],
            )
            if self._failure_count[url] >= _CIRCUIT_BREAKER_MAX_FAILURES:
                self._remove_url(url, f"circuit_breaker:{_CIRCUIT_BREAKER_MAX_FAILURES}_failures")
        start = (self.urls.index(url) + 1) if url in self.urls else self._index + 1
        nxt = self._next_available_index(start)
        if nxt is None:
            LOG.error("hunt proxy pool exhausted | pool_size=%d", len(self.urls))
            return None
        self._index = nxt
        active = self.current()
        LOG.info("hunt proxy failover | url=%s", mask_proxy_url(active))
        return active

    def _remove_url(self, url: str, reason: str) -> None:
        new_size = len(self.urls) - 1
        LOG.warning(
            "hunt proxy removed | url=%s reason=%s pool_size=%d",
            mask_proxy_url(url),
            reason,
            new_size,
        )
        if url in self.urls:
            self.urls = [u for u in self.urls if u != url]
        self._bad_until.pop(url, None)
        self._success_count.pop(url, None)
        self._failure_count.pop(url, None)
        self._success_streak.pop(url, None)
        self._last_latencies.pop(url, None)
        if self._index >= len(self.urls) and self.urls:
            self._index = self._index % len(self.urls)
        if new_size <= _MIN_POOL_SIZE:
            LOG.error("hunt proxy pool low | size=%d min=%d", new_size, _MIN_POOL_SIZE)

    def mark_success(self, url: str, latency_ms: float | None = None) -> None:
        if url in self.urls:
            self._success_count[url] = self._success_count.get(url, 0) + 1
            self._failure_count[url] = 0
            self._success_streak[url] = self._success_streak.get(url, 0) + 1
            if latency_ms is not None:
                samples = self._last_latencies.setdefault(url, [])
                samples.append(latency_ms)
                if len(samples) > self._rolling_window:
                    samples.pop(0)

    def _health_score(self, url: str) -> float:
        success = float(self._success_count.get(url, 0))
        failure = float(self._failure_count.get(url, 0))
        total = success + failure
        if total <= 0.0:
            return 1.0 if self._is_available(url) else 0.0
        base = success / total
        if not self._is_available(url):
            return base * 0.25
        streak = float(self._success_streak.get(url, 0))
        streak_bonus = min(0.3, streak * 0.1)
        lat_samples = self._last_latencies.get(url, [])
        if lat_samples:
            avg_lat = sum(lat_samples) / len(lat_samples)
            if avg_lat < 100:
                latency_bonus = 0.2
            elif avg_lat < 300:
                latency_bonus = 0.15
            elif avg_lat < 800:
                latency_bonus = 0.1
            elif avg_lat < 2000:
                latency_bonus = 0.05
            else:
                latency_bonus = 0.0
        else:
            latency_bonus = 0.0
        return min(1.0, base + streak_bonus + latency_bonus)

    def reset(self, urls: list[str]) -> None:
        cleaned = _dedupe_urls(urls)
        if not cleaned:
            return
        self.urls = cleaned
        self._bad_until.clear()
        self._success_count.clear()
        self._failure_count.clear()
        self._success_streak.clear()
        self._last_latencies.clear()
        self._index = 0

    async def revalidate(
        self,
        validate_fn: Callable[[str, asyncio.Semaphore, float], tuple[str, float] | None],
        *,
        concurrency: int = 20,
        timeout_s: float = 10.0,
    ) -> int:
        if not self.urls:
            return 0
        sem = asyncio.Semaphore(min(concurrency, len(self.urls)))
        results = await asyncio.gather(
            *[validate_fn(u, sem, timeout_s) for u in self.urls],
            return_exceptions=True,
        )
        dead: list[str] = []
        for url, result in zip(self.urls, results, strict=False):
            if not isinstance(result, tuple):
                dead.append(url)
        for url in dead:
            self._remove_url(url, "revalidate_failed")
        return len(dead)

    def rotate_after_failure(self, failed_url: str | None, reason: str) -> str | None:
        if not self.has_alternatives():
            return None
        return self.mark_failed(failed_url or self.current(), reason)


def _dedupe_urls(urls: list[str]) -> list[str]:
    out: list[str] = []
    for raw in urls:
        value = str(raw or "").strip()
        if value and value not in out:
            out.append(value)
    return out


def is_proxy_transport_error(exc: BaseException) -> bool:
    name = exc.__class__.__name__
    if name in {"ProxyConnectionError", "ProxyTimeoutError", "ProxyError"}:
        return True
    message = str(exc).lower()
    markers = (
        "couldn't connect to proxy",
        "proxy connection refused",
        "proxy timed out",
        "name or service not known",
        "getaddrinfo failed",
    )
    return any(marker in message for marker in markers)



LOG = logging.getLogger("hunt_core.market.network")

_FAPI_EXCHANGE_INFO = "https://fapi.binance.com/fapi/v1/exchangeInfo"
_LOCAL_CANDIDATES = (
    "socks5://127.0.0.1:7890",
    "socks5://127.0.0.1:7891",
    "socks5://127.0.0.1:10808",
    "socks5://127.0.0.1:1080",
    "socks5://127.0.0.1:9050",
    "http://127.0.0.1:7890",
    "http://127.0.0.1:8080",
)
_PROBE_TIMEOUT = aiohttp.ClientTimeout(total=12.0, connect=8.0)
_PROBE_CONCURRENCY = 20
_MIN_SYMBOLS = 100
_MAX_WORKING = 12


async def _probe_exchange_info(proxy_url: str | None) -> bool:
    session = create_aiohttp_session(
        proxy_url=proxy_url,
        trust_env=False,
        timeout=_PROBE_TIMEOUT,
        connector_limit=4,
    )
    req_proxy = aiohttp_request_proxy(session, proxy_url)
    try:
        async with session.get(_FAPI_EXCHANGE_INFO, proxy=req_proxy) as resp:
            if resp.status != 200:
                return False
            payload = await resp.json(content_type=None)
        symbols = payload.get("symbols") if isinstance(payload, dict) else None
        ok = isinstance(symbols, list) and len(symbols) > _MIN_SYMBOLS
        if ok:
            LOG.info(
                "hunt proxy ok | url=%s symbols=%d",
                mask_proxy_url(proxy_url or "direct"),
                len(symbols),
            )
        return ok
    except (KeyboardInterrupt, SystemExit, asyncio.CancelledError):
        raise
    except Exception as exc:
        LOG.debug(
            "hunt proxy fail | url=%s err=%s",
            mask_proxy_url(proxy_url or "direct"),
            type(exc).__name__,
        )
        return False
    finally:
        await close_aiohttp_session(session)


async def auto_discover_proxies(*, include_public: bool = False) -> list[str]:
    """Return working proxy URLs for hunt CCXT plane."""

    working: list[str] = []

    if await _probe_exchange_info(None):
        LOG.info("hunt direct binance access works")

    env_proxy = resolve_proxy_url(config_url=None, trust_env=True)
    if env_proxy and await _probe_exchange_info(env_proxy) and env_proxy not in working:
        working.append(env_proxy)

    for candidate in _LOCAL_CANDIDATES:
        if candidate in working:
            continue
        if await _probe_exchange_info(candidate):
            working.append(candidate)
        if len(working) >= _MAX_WORKING:
            return working

    if not include_public:
        return working

    candidates = await _fetch_public_candidates()
    sem = asyncio.Semaphore(_PROBE_CONCURRENCY)

    async def _one(url: str) -> str | None:
        async with sem:
            if await _probe_exchange_info(url):
                return url
        return None

    results = await asyncio.gather(*[_one(url) for url in candidates])
    for url in results:
        if url and url not in working:
            working.append(url)
        if len(working) >= _MAX_WORKING:
            break
    return working


async def _fetch_public_candidates() -> list[str]:
    list_urls = (
        "https://raw.githubusercontent.com/proxifly/free-proxy-list/main/proxies/protocols/socks5/data.txt",
        "https://raw.githubusercontent.com/proxifly/free-proxy-list/main/proxies/protocols/http/data.txt",
    )
    merged: list[str] = []
    async with aiohttp.ClientSession(timeout=_PROBE_TIMEOUT) as session:
        for url in list_urls:
            try:
                async with session.get(url) as resp:
                    if resp.status != 200:
                        continue
                    text = await resp.text()
                    scheme = "socks5" if "socks5" in url else "http"
                    for item in _parse_host_port_lines(text):
                        if not item.startswith("http"):
                            merged.append(
                                f"{scheme}://{item.split('://', 1)[-1]}"
                                if "://" not in item
                                else item
                            )
                        else:
                            merged.append(item)
            except Exception:
                continue
    deduped: list[str] = []
    for item in merged:
        if item not in deduped:
            deduped.append(item)
    return deduped[:200]


def _parse_host_port_lines(text: str) -> list[str]:
    out: list[str] = []
    for raw_line in text.splitlines():
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "://" in stripped:
            out.append(normalize_proxy_url(stripped))
            continue
        if re.match(r"^\d{1,3}(\.\d{1,3}){3}:\d+$", stripped):
            host, port = stripped.rsplit(":", 1)
            scheme = "socks5" if int(port) not in {80, 8080, 3128, 8888} else "http"
            out.append(f"{scheme}://{host}:{port}")
    return out


def write_proxies_to_config(path: Path, urls: list[str], *, direct_ok: bool = False) -> None:
    """Update ``[bot.network]`` in config.toml with discovered proxies."""
    text = path.read_text(encoding="utf-8")
    block = _render_network_block(urls, direct_ok=direct_ok)
    pattern = re.compile(r"(?:#[^\n]*\n)*\[bot\.network\].*?(?=\n\[|\Z)", re.DOTALL)
    if pattern.search(text):
        text = pattern.sub(block, text, count=1)
    else:
        text = text.rstrip() + "\n\n" + block + "\n"
    tmp_path = path.with_name(f"{path.name}.tmp")
    tmp_path.write_text(text, encoding="utf-8")
    tmp_path.replace(path)
    LOG.info("hunt config proxies updated | path=%s endpoints=%d", path, len(urls))


def _render_network_block(urls: list[str], *, direct_ok: bool) -> str:
    lines = [
        "# Auto-discovered by hunt_core.market.network",
        "[bot.network]",
        f"trust_env = {str(direct_ok).lower()}",
        "failover_enabled = true",
        "failover_cooldown_seconds = 120",
        'proxy_url = ""',
        "proxy_urls = [",
    ]
    for url in urls:
        lines.append(f'  "{url}",')
    lines.append("]")
    return "\n".join(lines)

__all__ = [
    "BanDetectionPolicy",
    "ProxyPool",
    "is_proxy_transport_error",
]
