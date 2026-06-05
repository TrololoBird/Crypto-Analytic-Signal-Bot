"""Discover working Binance egress proxies and write them into config.toml."""

from __future__ import annotations

import argparse
import asyncio
import re
import time
from pathlib import Path

import aiohttp

try:
    from scripts.common import bootstrap_repo_path, configure_script_logging
except ModuleNotFoundError:  # pragma: no cover
    from common import bootstrap_repo_path, configure_script_logging

from bot.runtime.errors import DEFENSIVE_EXC
from bot.domain.config import NetworkConfig
from bot.market.data import BinanceFuturesMarketData, MarketDataUnavailable
from bot.market.network_proxy import mask_proxy_url, normalize_proxy_url, resolve_proxy_url
from bot.market.rest_impl import BinanceClientImpl

LOG = configure_script_logging("scripts.discover_binance_proxies")

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
_PUBLIC_LIST_URLS = (
    "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/socks5.txt",
    "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt",
    "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/socks5.txt",
    "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/http.txt",
)
_PROBE_CONCURRENCY = 24
_PROBE_TIMEOUT = aiohttp.ClientTimeout(total=12.0, connect=8.0)
_MAX_PUBLIC_TO_TEST = 80
_MAX_WORKING = 8


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


async def _fetch_public_candidates(session: aiohttp.ClientSession) -> list[str]:
    merged: list[str] = []
    for url in _PUBLIC_LIST_URLS:
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=20)) as resp:
                if resp.status != 200:
                    continue
                text = await resp.text()
                scheme = "socks5" if "socks5" in url else "http"
                for item in _parse_host_port_lines(text):
                    if not item.startswith("http"):
                        merged.append(
                            f"{scheme}://{item.split('://', 1)[-1]}" if "://" not in item else item
                        )
                    else:
                        merged.append(item)
        except DEFENSIVE_EXC as exc:
            LOG.debug("proxy list fetch failed | url=%s err=%s", url, exc)
    deduped: list[str] = []
    for item in merged:
        if item not in deduped:
            deduped.append(item)
    return deduped[:_MAX_PUBLIC_TO_TEST]


async def _probe_url(url: str | None, *, trust_env: bool) -> bool:
    if url:
        net = NetworkConfig(proxy_url=url, trust_env=False, failover_enabled=False)
    else:
        net = NetworkConfig(trust_env=trust_env, failover_enabled=False)
    client = BinanceClientImpl(rest_timeout_seconds=12.0, network=net)
    market = BinanceFuturesMarketData(binance_client=client)
    try:
        symbols = await market.fetch_exchange_symbols()
        ok = len(symbols) > 100
        if ok:
            LOG.info("proxy ok | url=%s symbols=%d", mask_proxy_url(url or "direct"), len(symbols))
    except MarketDataUnavailable as exc:
        LOG.debug(
            "proxy fail | url=%s detail=%s",
            mask_proxy_url(url or "direct"),
            exc.detail,
        )
        return False
    except DEFENSIVE_EXC as exc:
        if isinstance(exc, (KeyboardInterrupt, SystemExit, asyncio.CancelledError)):
            raise
        LOG.debug(
            "proxy transport fail | url=%s err=%s",
            mask_proxy_url(url or "direct"),
            exc,
        )
        return False
    else:
        return ok
    finally:
        await market.close()


async def _quick_http_probe(session: aiohttp.ClientSession, proxy_url: str) -> bool:
    """Fast filter before full Binance client probe."""
    try:
        async with session.get(
            _FAPI_EXCHANGE_INFO,
            proxy=proxy_url if proxy_url.startswith("http") else None,
            timeout=_PROBE_TIMEOUT,
        ) as resp:
            if resp.status != 200:
                return False
            payload = await resp.json(content_type=None)
            return isinstance(payload, dict) and bool(payload.get("symbols"))
    except DEFENSIVE_EXC:
        return False


async def _discover(
    *,
    include_public: bool,
    trust_env: bool,
) -> tuple[list[str], bool]:
    working: list[str] = []
    direct_ok = await _probe_url(None, trust_env=False)
    if direct_ok:
        LOG.info("direct binance access works")

    env_proxy = resolve_proxy_url(config_url=None, trust_env=trust_env)
    if env_proxy and await _probe_url(env_proxy, trust_env=False) and env_proxy not in working:
        working.append(env_proxy)

    for candidate in _LOCAL_CANDIDATES:
        if candidate in working:
            continue
        if await _probe_url(candidate, trust_env=False):
            working.append(candidate)
        if len(working) >= _MAX_WORKING:
            return working, direct_ok

    if not include_public:
        return working, direct_ok

    async with aiohttp.ClientSession(timeout=_PROBE_TIMEOUT) as session:
        candidates = await _fetch_public_candidates(session)
    LOG.info("testing public proxy candidates | count=%d", len(candidates))

    sem = asyncio.Semaphore(_PROBE_CONCURRENCY)
    passed_quick: list[str] = []

    async def _quick_one(url: str) -> None:
        async with sem:
            if url.startswith("http"):
                async with aiohttp.ClientSession(timeout=_PROBE_TIMEOUT) as http_sess:
                    if await _quick_http_probe(http_sess, url):
                        passed_quick.append(url)
            else:
                if await _probe_url(url, trust_env=False):
                    passed_quick.append(url)

    await asyncio.gather(*[_quick_one(url) for url in candidates])

    for url in passed_quick:
        if url in working:
            continue
        if url.startswith("http"):
            if await _probe_url(url, trust_env=False):
                working.append(url)
        elif url not in working:
            working.append(url)
        if len(working) >= _MAX_WORKING:
            break

    return working, direct_ok


def _update_config_toml(path: Path, urls: list[str], *, direct_ok: bool = False) -> None:
    text = path.read_text(encoding="utf-8")
    block = _render_network_block(urls, direct_ok=direct_ok)
    pattern = re.compile(r"\[bot\.network\][^\[]*", re.DOTALL)
    if pattern.search(text):
        text = pattern.sub(block + "\n", text, count=1)
    else:
        text = text.rstrip() + "\n\n" + block + "\n"
    path.write_text(text, encoding="utf-8")
    LOG.info("config updated | path=%s endpoints=%d", path, len(urls))


def _render_network_block(urls: list[str], *, direct_ok: bool = False) -> str:
    if not urls:
        return """[bot.network]
proxy_url = ""
proxy_urls = []
trust_env = true
failover_enabled = true
failover_cooldown_seconds = 300"""
    if direct_ok:
        primary = ""
        rest = urls
    else:
        primary = urls[0]
        rest = urls[1:]
    lines = [
        "# Auto-discovered by scripts/discover_binance_proxies.py",
        "[bot.network]",
        f'proxy_url = "{primary}"' if primary else 'proxy_url = ""',
        "proxy_urls = [",
    ]
    lines.extend(f'  "{item}",' for item in rest)
    lines.append("]")
    lines.extend(
        [
            "trust_env = true",
            "failover_enabled = true",
            "failover_cooldown_seconds = 300",
        ]
    )
    return "\n".join(lines)


def main() -> None:
    bootstrap_repo_path()
    parser = argparse.ArgumentParser(description="Discover Binance proxies and update config.toml")
    parser.add_argument("--config", default="config.toml", help="Path to config.toml")
    parser.add_argument(
        "--no-public",
        action="store_true",
        help="Only local ports and env (skip public list fetch)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print results without writing config",
    )
    args = parser.parse_args()
    started = time.monotonic()
    urls, direct_ok = asyncio.run(_discover(include_public=not args.no_public, trust_env=False))
    elapsed = time.monotonic() - started

    if not urls and not direct_ok:
        LOG.error("no binance path (direct or proxy) in %.1fs", elapsed)
        raise SystemExit(2)

    if not urls:
        LOG.warning(
            "no proxy endpoints in %.1fs — direct-only (trust_env failover)",
            elapsed,
        )
        if not args.dry_run:
            config_path = Path(args.config)
            if config_path.is_file():
                _update_config_toml(config_path, [], direct_ok=True)
        raise SystemExit(0)

    LOG.info(
        "discovery complete | working=%d elapsed=%.1fs primary=%s",
        len(urls),
        elapsed,
        mask_proxy_url(urls[0]),
    )
    for url in urls:
        print(url)

    if args.dry_run:
        return

    config_path = Path(args.config)
    if not config_path.is_file():
        LOG.error("config missing | path=%s", config_path)
        raise SystemExit(1)
    _update_config_toml(config_path, urls, direct_ok=direct_ok)


if __name__ == "__main__":
    main()
