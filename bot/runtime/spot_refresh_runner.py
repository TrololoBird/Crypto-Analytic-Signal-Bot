"""Periodic spot companion refresh for shortlist symbols."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from bot.runtime.errors import DEFENSIVE_EXC
from bot.market.spot_companion import SpotCompanionService

LOG = logging.getLogger("bot.runtime.spot_refresh_runner")


class SpotRefreshRunner:
    def __init__(self, bot: Any) -> None:
        self._bot = bot
        self._service: SpotCompanionService | None = None

    def _settings(self) -> Any:
        return getattr(self._bot, "settings", None)

    def _companion_cfg(self) -> Any:
        settings = self._settings()
        return getattr(settings, "spot_companion", None) if settings is not None else None

    def enabled(self) -> bool:
        cfg = self._companion_cfg()
        return bool(cfg is not None and getattr(cfg, "enabled", False))

    def _service_instance(self) -> SpotCompanionService:
        if self._service is None:
            settings = self._settings()
            network = getattr(settings, "network", None)
            self._service = SpotCompanionService(
                base_url=str(getattr(cfg, "base_url", "") or "https://data-api.binance.vision"),
                proxy_url=getattr(network, "proxy_url", None),
                trust_env=bool(getattr(network, "trust_env", True)),
            )
        return self._service

    def enrichments_for(self, symbol: str) -> dict[str, float]:
        if not self.enabled():
            return {}
        service = self._service
        if service is None:
            return {}
        cfg = self._companion_cfg()
        max_age = float(getattr(cfg, "refresh_interval_seconds", 60) or 60)
        return service.enrichments_for(symbol, max_age_seconds=max_age)

    async def refresh_once(self, shortlist: list[Any]) -> int:
        if not self.enabled():
            return 0
        cfg = self._companion_cfg()
        lead_symbols = {
            str(item).strip().upper()
            for item in getattr(cfg, "lead_symbols", ()) or ()
            if str(item).strip()
        }
        symbols: list[str] = []
        futures_mid_by_symbol: dict[str, float | None] = {}
        for item in shortlist:
            symbol = str(getattr(item, "symbol", "") or "").strip().upper()
            if not symbol:
                continue
            if lead_symbols and symbol not in lead_symbols:
                continue
            symbols.append(symbol)
            last_price = getattr(item, "last_price", None)
            try:
                futures_mid_by_symbol[symbol] = float(last_price) if last_price else None
            except (TypeError, ValueError):
                futures_mid_by_symbol[symbol] = None
        if not symbols:
            return 0
        try:
            return await self._service_instance().refresh_symbols(
                symbols,
                futures_mid_by_symbol=futures_mid_by_symbol,
            )
        except DEFENSIVE_EXC as exc:
            LOG.debug("spot refresh batch failed: %s", exc)
            return 0

    async def run(self) -> None:
        cfg = self._companion_cfg()
        if not self.enabled() or cfg is None:
            return
        interval = max(30, int(getattr(cfg, "refresh_interval_seconds", 60) or 60))
        await asyncio.sleep(15)
        while not self._bot._shutdown.is_set():
            try:
                async with self._bot._shortlist_lock:
                    shortlist = list(self._bot._shortlist)
                if shortlist:
                    updated = await self.refresh_once(shortlist)
                    if updated:
                        LOG.debug("spot companion refreshed | symbols=%d", updated)
            except DEFENSIVE_EXC as exc:
                LOG.debug("spot companion periodic refresh failed: %s", exc)
            try:
                await asyncio.wait_for(self._bot._shutdown.wait(), timeout=interval)
            except TimeoutError:
                continue

    async def close(self) -> None:
        if self._service is not None:
            await self._service.close()
