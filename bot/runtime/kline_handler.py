from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any

from ..domain.events import KlineCloseEvent
from ..domain.schemas import PipelineResult, Signal

LOG = logging.getLogger("bot.runtime.kline_handler")


class KlineHandler:
    """Handles kline-close orchestration and per-symbol selection/delivery."""

    def __init__(self, bot: Any) -> None:
        self._bot = bot

    async def on_kline_close(self, event: KlineCloseEvent) -> None:
        try:
            await self._process_kline(event)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            LOG.error("kline_handler_error", exc_info=exc, extra={"symbol": event.symbol})

    async def _process_kline(self, event: KlineCloseEvent) -> None:
        allowed = tuple(
            getattr(self._bot.settings.runtime, "analysis_kline_intervals", None) or ("15m",)
        )
        if event.interval not in allowed:
            return

        self._bot._last_kline_event_ts = asyncio.get_running_loop().time()
        symbol = event.symbol
        LOG.info("kline_close received | symbol=%s trigger=%s", symbol, event.trigger)

        async with self._bot._shortlist_lock:
            shortlist = list(self._bot._shortlist)

        tracking_events = await self._bot.tracker.review_open_signals_for_symbol(
            symbol, dry_run=False
        )
        if tracking_events:
            await self._bot._deliver_tracking(tracking_events)

        item = next((row for row in shortlist if row.symbol == symbol), None)
        if item is None:
            LOG.debug("kline_close skipped | symbol=%s not in shortlist", symbol)
            return

        await self._bot._get_cycle_runner().execute_symbol_cycle(
            symbol=symbol,
            item=item,
            interval=event.interval,
            trigger=event.trigger,
            event_ts=datetime.now(UTC),
            tracking_events=tracking_events,
            shortlist_size=len(shortlist),
        )

    async def select_and_deliver_for_symbol(
        self,
        symbol: str,
        result: PipelineResult,
    ) -> tuple[list[Signal], list[dict[str, Any]], list[Signal]]:
        candidates = result.candidates
        rejected: list[dict[str, Any]] = list(result.rejected)
        delivered: list[Signal] = []

        if candidates:
            selected = self._bot._select_and_rank(
                {symbol: candidates},
                max_signals=self._bot.settings.runtime.max_signals_per_cycle,
            )
            if result.funnel:
                result.funnel["selected"] = len(selected)
            prepared_by_tracking_id = (
                {item.tracking_id: result.prepared for item in selected}
                if result.prepared is not None
                else None
            )
            (
                delivered,
                cooldown_rejected,
                delivery_status_counts,
            ) = await self._bot._select_and_deliver(
                selected,
                prepared_by_tracking_id=prepared_by_tracking_id,
            )
            if result.funnel:
                result.funnel["delivered"] = len(delivered)
                result.funnel["delivery_status_counts"] = dict(delivery_status_counts)
            rejected.extend(cooldown_rejected)

        return candidates, rejected, delivered
