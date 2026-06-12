#!/usr/bin/env python3
"""One-shot market cross-section survey + calibrated hunt parameters."""

from __future__ import annotations

import asyncio
import json
import sys

from hunt_watch.bootstrap import bootstrap

bootstrap()

from hunt_watch.market_regime import refresh_market_regime

from engine.domain.config import load_settings
from hunt_core.market import HuntCcxtClient


async def main() -> None:
    settings = load_settings()
    client = HuntCcxtClient.from_settings(settings)
    try:
        snap = await refresh_market_regime(client)
        print(json.dumps(snap.to_dict(), indent=2, default=str))
    finally:
        await client.close()


if __name__ == "__main__":
    asyncio.run(main())
    sys.exit(0)
