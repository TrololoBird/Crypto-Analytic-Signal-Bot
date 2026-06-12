"""Factory for hunt CCXT market plane."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from hunt_core.market.client import HuntCcxtClient
from hunt_core.market.spot import HuntCcxtSpotCompanion
from hunt_core.market.streams import HuntCcxtStreams


@dataclass(slots=True)
class HuntMarketPlane:
    client: HuntCcxtClient
    streams: HuntCcxtStreams
    spot: HuntCcxtSpotCompanion


async def create_hunt_market_plane(
    *,
    proxy_url: str | None = None,
    trust_env: bool = True,
) -> HuntMarketPlane:
    """Build client + streams + spot; loads futures markets once."""
    client = HuntCcxtClient(proxy_url=proxy_url, trust_env=trust_env)
    await client.load_markets()
    streams = HuntCcxtStreams(client=client)
    spot = HuntCcxtSpotCompanion(proxy_url=proxy_url, trust_env=trust_env)
    return HuntMarketPlane(client=client, streams=streams, spot=spot)


async def create_hunt_market_plane_from_settings(settings: Any) -> HuntMarketPlane:
    net = getattr(settings, "network", settings)
    return await create_hunt_market_plane(
        proxy_url=getattr(net, "proxy_url", None),
        trust_env=getattr(net, "trust_env", True),
    )
