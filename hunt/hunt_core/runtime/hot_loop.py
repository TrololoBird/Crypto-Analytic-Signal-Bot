"""Dedicated hot-path loop — kline-close events without 3s idle polling."""
from __future__ import annotations

import asyncio
import os
from collections.abc import Awaitable, Callable
from typing import Any

from hunt_core.runtime.logging import configure_script_logging
from hunt_core.runtime.state import should_stop

LOG = configure_script_logging("hunt_core.runtime.hot_loop")

_HOT_POLL_S = float(os.getenv("HUNT_HOT_POLL_S", "0.35") or 0.35)
RunHotTickFn = Callable[[tuple[str, ...], dict[str, Any]], Awaitable[list[dict[str, Any]]]]


class HotKlineLoop:
    """Background task: 1m kline close → hot run_tick (parallel to cold 30s tick)."""

    def __init__(self, *, run_hot_tick: RunHotTickFn) -> None:
        self._run_hot_tick = run_hot_tick
        self._tick_ctx: dict[str, Any] | None = None
        self._task: asyncio.Task[None] | None = None

    def set_tick_ctx(self, ctx: dict[str, Any] | None) -> None:
        self._tick_ctx = ctx

    def start(self, ws_feed: Any, *, once: bool) -> None:
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(
            self._run(ws_feed, once=once),
            name="hunt_hot_kline_loop",
        )
        LOG.info("hot_kline_loop_started", poll_s=_HOT_POLL_S)

    async def stop(self) -> None:
        task = self._task
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        self._task = None

    async def _run(self, ws_feed: Any, *, once: bool) -> None:
        from hunt_core.data.frame_cache import get_frame_cache
        from hunt_core.data.lake import buffer_tick_rows
        from hunt_core.runtime.hot_eligible import filter_kline_hot_symbols
        from hunt_core.runtime.tick_state import last_tick_store
        from hunt_core.track.tracker import iter_active_tracker_symbols, load_tracker_state

        while not should_stop():
            if once:
                return
            ctx = self._tick_ctx
            if ctx is None:
                await asyncio.sleep(_HOT_POLL_S)
                continue
            pending = ws_feed.pop_kline_close_triggers()
            if not pending:
                active = list(ctx.get("active") or [])[:24]
                if active:
                    try:
                        await get_frame_cache().refresh_enrichment_batch(
                            ctx["client"],
                            active,
                            ws_feed=ws_feed,
                            limit=3,
                        )
                    except Exception:
                        LOG.debug("hot_enrichment_bg_failed", exc_info=True)
                await asyncio.sleep(_HOT_POLL_S)
                continue
            tracker_pin = load_tracker_state()
            tracker_active = {s.upper() for s, _ in iter_active_tracker_symbols(tracker_pin)}
            fast_syms = filter_kline_hot_symbols(
                tuple(s for s in (ctx.get("active") or ()) if s in pending),
                ignition_by_sym=ctx.get("ignition_by_sym"),
                tracker_active=tracker_active,
                last_tick_get=last_tick_store().get,
            )
            skipped = set(pending) - set(fast_syms)
            if skipped:
                LOG.debug(
                    "watch_kline_hot_filtered",
                    pending=len(pending),
                    eligible=len(fast_syms),
                    skipped=sorted(skipped)[:6],
                )
            if not fast_syms:
                ws_feed.consume_kline_close_triggers(set(pending))
                await asyncio.sleep(_HOT_POLL_S)
                continue
            LOG.info("watch_kline_1m_trigger", symbols=list(fast_syms), source="hot_loop")
            try:
                fast_rows = await self._run_hot_tick(fast_syms, ctx)
                for row in fast_rows:
                    row["tick_trigger"] = "kline_1m"
                buffer_tick_rows(fast_rows)
                ws_feed.consume_kline_close_triggers(set(fast_syms))
                if skipped:
                    ws_feed.consume_kline_close_triggers(skipped)
            except Exception:
                LOG.exception("watch_kline_fast_tick_failed")
            await asyncio.sleep(_HOT_POLL_S)


__all__ = ["HotKlineLoop"]
