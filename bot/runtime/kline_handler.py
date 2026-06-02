from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any

from ..domain.events import KlineCloseEvent
from ..domain.strategies import StrategyMetadata
from ..domain.schemas import PipelineResult, Signal
from ..market.scheduler import analysis_intervals

LOG = logging.getLogger("bot.runtime.kline_handler")


class KlineHandler:
    """Handles kline-close orchestration and per-symbol selection/delivery."""

    def __init__(self, bot: Any) -> None:
        self._bot = bot
        self._allowed_intervals_cache: frozenset[str] = frozenset()
        self._allowed_intervals_snapshot: (
            tuple[
                tuple[str, ...],
                tuple[tuple[str, str, tuple[str, ...], tuple[str, ...], tuple[str, ...]], ...],
            ]
            | None
        ) = None

    async def on_kline_close(self, event: KlineCloseEvent) -> None:
        try:
            await self._process_kline(event)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            LOG.error("kline_handler_error", exc_info=exc, extra={"symbol": event.symbol})

    async def _process_kline(self, event: KlineCloseEvent) -> None:
        if event.interval not in self._allowed_intervals():
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

    def _allowed_intervals(self) -> frozenset[str]:
        runtime_intervals = analysis_intervals(self._bot.settings)
        registry = getattr(self._bot, "_modern_registry", None)
        strategy_snapshot: tuple[
            tuple[str, str, tuple[str, ...], tuple[str, ...], tuple[str, ...]],
            ...,
        ] = ()
        strategy_interval_union: tuple[str, ...] = ()
        if registry is not None:
            enabled_metas = registry.list_enabled()
            strategy_snapshot, strategy_interval_union = self._strategy_interval_state(
                enabled_metas
            )
        snapshot = (runtime_intervals, strategy_snapshot)
        if snapshot != self._allowed_intervals_snapshot:
            intervals = set(runtime_intervals)
            intervals.update(strategy_interval_union)
            self._allowed_intervals_cache = frozenset(intervals)
            self._allowed_intervals_snapshot = snapshot
        return self._allowed_intervals_cache

    def _strategy_interval_state(
        self, enabled_metas: list[StrategyMetadata]
    ) -> tuple[
        tuple[tuple[str, str, tuple[str, ...], tuple[str, ...], tuple[str, ...]], ...],
        tuple[str, ...],
    ]:
        records: list[tuple[str, str, tuple[str, ...], tuple[str, ...], tuple[str, ...]]] = []
        interval_union: set[str] = set()
        for meta in enabled_metas:
            trigger_tf = str(getattr(meta, "trigger_tf", None) or "15m").strip()
            trigger_intervals = tuple(
                dict.fromkeys(
                    str(x).strip()
                    for x in (getattr(meta, "trigger_intervals", None) or ())
                    if str(x).strip()
                )
            )
            required_tfs = tuple(
                dict.fromkeys(
                    str(x).strip()
                    for x in (getattr(meta, "required_tfs", None) or ())
                    if str(x).strip()
                )
            )
            timeframes = tuple(
                dict.fromkeys(
                    str(x).strip()
                    for x in (getattr(meta, "timeframes", None) or ())
                    if str(x).strip()
                )
            )
            records.append(
                (meta.strategy_id, trigger_tf, trigger_intervals, required_tfs, timeframes)
            )
            interval_union.add(trigger_tf)
            interval_union.update(trigger_intervals)
            interval_union.update(required_tfs)
            interval_union.update(timeframes)
        records.sort(key=lambda item: item[0])
        return tuple(records), tuple(sorted(interval_union))

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
