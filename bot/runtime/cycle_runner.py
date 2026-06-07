"""Signal cycle execution helpers for SignalBot."""

from __future__ import annotations

import asyncio
import logging
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast

from bot.domain.delivery_policy import KLINE_CLOSE_ONLY_SETUP_IDS
from bot.market.data import BinanceFuturesMarketData, MarketDataUnavailable
from bot.runtime.data_readiness import is_radar_promoted_item, missing_derivatives_context
from bot.runtime.delivery_orchestrator import DELIVERY_SUCCESS_STATUSES
from bot.runtime.errors import DEFENSIVE_EXC

from ..domain.schemas import PipelineResult, PreparedSymbol, Signal, SymbolFrames, UniverseSymbol

LOG = logging.getLogger("bot.runtime.cycle_runner")


@dataclass(slots=True)
class CycleContext:
    frames: SymbolFrames
    ws_enrichments: dict[str, Any]


class CycleRunner:
    """Encapsulates signal-cycle execution while SignalBot keeps orchestration."""

    def __init__(self, bot: Any) -> None:
        self._bot = bot

    @staticmethod
    def _intra_candle_detector_limits(bot: Any) -> tuple[int | None, frozenset[str] | None]:
        ws = bot.settings.ws
        subset = frozenset(ws.intra_candle_setup_subset) if ws.intra_candle_setup_subset else None
        max_setups = ws.intra_candle_max_setups if ws.intra_candle_max_setups > 0 else None
        return max_setups, subset

    async def _prepare_cycle_context(
        self,
        *,
        item: UniverseSymbol,
        symbol: str,
        ws_enrichments_override: dict[str, Any] | None = None,
        include_spot_enrichments: bool = True,
        require_derivatives: bool = True,
    ) -> CycleContext | None:
        bot = self._bot
        frames = await bot._fetch_frames(item)
        if frames is None:
            return None

        ws_enrichments = dict(bot._ws_cache_enrichments(symbol))
        if ws_enrichments_override:
            ws_enrichments.update(ws_enrichments_override)
        if include_spot_enrichments:
            ws_enrichments.update(bot._spot_enrichments(symbol))

        if ws_enrichments.get("ticker_price") is None and isinstance(
            bot.client, BinanceFuturesMarketData
        ):
            cached_price = bot.client.get_cached_symbol_price(symbol)
            if cached_price is not None and cached_price > 0.0:
                ws_enrichments["ticker_price"] = cached_price
            else:
                try:
                    rest_price = await bot.client.fetch_symbol_price(symbol)
                except MarketDataUnavailable:
                    rest_price = None
                if rest_price is not None and rest_price > 0.0:
                    ws_enrichments["ticker_price"] = rest_price

        if require_derivatives and missing_derivatives_context(ws_enrichments):
            try:
                warmed = await bot._get_oi_refresh_runner().refresh_symbol_if_missing(
                    symbol,
                    max_age_seconds=900.0,
                    include_funding_history=True,
                    timeout_seconds=bot.settings.runtime.emergency_context_fetch_timeout_seconds,
                )
                if warmed:
                    ws_enrichments.update(bot._ws_cache_enrichments(symbol))
            except DEFENSIVE_EXC as exc:
                LOG.info(
                    "symbol derivatives context warmup skipped | symbol=%s error=%s",
                    symbol,
                    exc,
                )

        if require_derivatives and missing_derivatives_context(ws_enrichments):
            still_missing = missing_derivatives_context(ws_enrichments)
            bot.telemetry.append_jsonl(
                "rejected.jsonl",
                {
                    "ts": datetime.now(UTC).isoformat(),
                    "symbol": symbol,
                    "setup_id": "data",
                    "direction": "none",
                    "stage": "data",
                    "reason": "derivatives_context_missing",
                    "missing_fields": still_missing,
                },
            )
            return None

        return CycleContext(frames=frames, ws_enrichments=ws_enrichments)

    async def execute_symbol_cycle(
        self,
        *,
        symbol: str,
        item: UniverseSymbol,
        interval: str,
        trigger: str,
        event_ts: datetime,
        tracking_events: list[Any],
        shortlist_size: int,
        ws_enrichments_override: dict[str, Any] | None = None,
    ) -> None:
        bot = self._bot
        runtime = getattr(bot.settings, "runtime", bot.settings)
        timeout = float(
            getattr(
                runtime,
                "cycle_timeout_seconds",
                60.0,  # seconds: fallback per-symbol cycle budget
            )
        )
        try:
            await asyncio.wait_for(
                self._execute_symbol_cycle_unbounded(
                    symbol=symbol,
                    item=item,
                    interval=interval,
                    trigger=trigger,
                    event_ts=event_ts,
                    tracking_events=tracking_events,
                    shortlist_size=shortlist_size,
                    ws_enrichments_override=ws_enrichments_override,
                ),
                timeout=timeout,
            )
        except TimeoutError:
            LOG.warning(
                "cycle_timeout | symbol=%s timeout_seconds=%.1f trigger=%s",
                symbol,
                timeout,
                trigger,
            )
            reject_row = {
                "ts": datetime.now(UTC).isoformat(),
                "symbol": symbol,
                "setup_id": "runtime",
                "direction": "none",
                "stage": "runtime",
                "reason": "runtime.cycle_timeout",
                "reason_code": "runtime.cycle_timeout",
                "timeout_seconds": timeout,
                "trigger": trigger,
                "event_interval": interval,
            }
            bot.telemetry.append_jsonl("rejected.jsonl", reject_row)
            timeout_result = PipelineResult(
                symbol=symbol,
                trigger=trigger,
                event_ts=event_ts,
                raw_setups=0,
                candidates=[],
                rejected=[reject_row],
                error=f"cycle_timeout after {timeout:.1f}s",
                status="cycle_timeout",
                funnel={
                    "cycle_timeout": True,
                    "timeout_seconds": timeout,
                    "kline_interval": interval,
                },
            )
            bot._emit_cycle_log(
                symbol=symbol,
                interval=interval,
                event_ts=event_ts,
                shortlist_size=shortlist_size,
                tracking_events=tracking_events,
                result=timeout_result,
                candidates=[],
                rejected=[reject_row],
                delivered=[],
            )

    async def _execute_symbol_cycle_unbounded(
        self,
        *,
        symbol: str,
        item: UniverseSymbol,
        interval: str,
        trigger: str,
        event_ts: datetime,
        tracking_events: list[Any],
        shortlist_size: int,
        ws_enrichments_override: dict[str, Any] | None = None,
    ) -> None:
        bot = self._bot
        max_setups: int | None = None
        setup_subset: frozenset[str] | None = None
        setup_exclude: frozenset[str] | None = None
        if trigger == "intra_candle":
            max_setups, setup_subset = self._intra_candle_detector_limits(bot)
            setup_exclude = KLINE_CLOSE_ONLY_SETUP_IDS

        async with bot._analysis_semaphore:
            context = await self._prepare_cycle_context(
                item=item,
                symbol=symbol,
                ws_enrichments_override=ws_enrichments_override,
                include_spot_enrichments=True,
                require_derivatives=True,
            )
            if context is None:
                return

            result = await bot._run_modern_analysis(
                item,
                context.frames,
                trigger=trigger,
                event_ts=event_ts,
                ws_enrichments=context.ws_enrichments,
                kline_interval=interval,
                max_setups=max_setups,
                setup_subset=setup_subset,
                setup_exclude=setup_exclude,
            )

            candidates, rejected, delivered = await bot._select_and_deliver_for_symbol(
                symbol,
                result,
            )

        for row in rejected:
            bot.telemetry.append_jsonl("rejected.jsonl", row)
        for sig in candidates:
            bot.telemetry.append_jsonl(
                "candidates.jsonl",
                {"ts": datetime.now(UTC).isoformat(), **sig.to_log_row()},
            )
        for sig in delivered:
            bot.telemetry.append_jsonl(
                "selected.jsonl",
                {"ts": datetime.now(UTC).isoformat(), **sig.to_log_row()},
            )

        bot._emit_cycle_log(
            symbol=symbol,
            interval=interval,
            event_ts=event_ts,
            shortlist_size=shortlist_size,
            tracking_events=tracking_events,
            result=result,
            candidates=candidates,
            rejected=rejected,
            delivered=delivered,
        )

    def _emergency_shortlist_for_scan(
        self, shortlist: list[UniverseSymbol]
    ) -> list[UniverseSymbol]:
        """Emergency fallback: radar-promoted symbols at half frequency (2x interval)."""
        bot = self._bot
        fallback_sec = float(bot.settings.runtime.emergency_fallback_seconds)
        min_interval = max(fallback_sec * 2.0, 60.0)
        now = asyncio.get_running_loop().time()
        last = getattr(bot, "_last_emergency_radar_scan", {})
        ready: list[UniverseSymbol] = []
        deferred: list[UniverseSymbol] = []
        for item in shortlist:
            if not is_radar_promoted_item(item):
                ready.append(item)
                continue
            if now - float(last.get(item.symbol, 0.0)) >= min_interval:
                last[item.symbol] = now
                ready.append(item)
            else:
                deferred.append(item)
        bot._last_emergency_radar_scan = last
        if deferred:
            LOG.debug(
                "emergency fallback deferred radar symbols | count=%d interval_s=%.0f",
                len(deferred),
                min_interval,
            )
        return ready

    async def run_emergency_cycle(self) -> dict[str, Any]:
        bot = self._bot
        tracking_events = await bot.tracker.review_open_signals(dry_run=False)
        if tracking_events:
            await bot._deliver_tracking(tracking_events)

        async with bot._shortlist_lock:
            shortlist = list(bot._shortlist)
        if not shortlist:
            shortlist = await bot._do_refresh_shortlist()
        shortlist = self._emergency_shortlist_for_scan(shortlist)
        if shortlist:
            try:
                runtime = bot.settings.runtime
                warmed = await bot._get_oi_refresh_runner().refresh_once(
                    shortlist,
                    max_age_seconds=300.0,
                    time_budget_seconds=runtime.emergency_context_warmup_timeout_seconds,
                    symbol_limit=runtime.emergency_context_warmup_symbol_limit,
                    include_funding_history=True,
                    per_symbol_timeout_seconds=runtime.emergency_context_fetch_timeout_seconds,
                )
                if warmed:
                    LOG.info(
                        (
                            "emergency cycle context warmup | symbols=%d budget_s=%.1f "
                            "symbol_limit=%d funding_history=true"
                        ),
                        warmed,
                        runtime.emergency_context_warmup_timeout_seconds,
                        runtime.emergency_context_warmup_symbol_limit,
                    )
            except DEFENSIVE_EXC:
                LOG.exception("emergency cycle context warmup failed")

        async def _analyze_one(item: UniverseSymbol) -> PipelineResult | None:
            async with bot._analysis_semaphore:
                context = await self._prepare_cycle_context(
                    item=item,
                    symbol=item.symbol,
                    include_spot_enrichments=False,
                    require_derivatives=False,
                )
                if context is None:
                    return None
                result = await bot._run_modern_analysis(
                    item,
                    context.frames,
                    trigger="emergency_fallback",
                    ws_enrichments=context.ws_enrichments,
                    kline_interval="emergency_fallback",
                )
                return cast("PipelineResult | None", result)

        tasks = [asyncio.create_task(_analyze_one(item)) for item in shortlist]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        pipeline_results: list[PipelineResult] = []
        all_candidates: dict[str, list[Signal]] = {}
        all_rejected: list[dict[str, Any]] = []
        rejected_by_symbol: dict[str, list[dict[str, Any]]] = {}
        bias_counter: Counter[str] = Counter()

        for res in results:
            if res is None or isinstance(res, Exception) or not isinstance(res, PipelineResult):
                continue
            pipeline_results.append(res)
            rejected_by_symbol.setdefault(res.symbol, [])
            rejected_by_symbol[res.symbol].extend(res.rejected)
            all_rejected.extend(res.rejected)
            for row in res.rejected:
                bot.telemetry.append_jsonl("rejected.jsonl", row)
            if res.error:
                continue
            all_candidates[res.symbol] = res.candidates
            if res.prepared is not None:
                bias_counter[res.prepared.bias_4h] += 1

        prepared_by_tracking_id: dict[str, PreparedSymbol] = {}
        for res in pipeline_results:
            if res.prepared is None:
                continue
            for signal in res.candidates:
                prepared_by_tracking_id[signal.tracking_id] = res.prepared

        await bot._delivery_orchestrator.preload_ranking_cooldowns(all_candidates)
        selected = bot._select_and_rank(
            all_candidates,
            max_signals=bot.settings.runtime.max_signals_per_cycle,
        )
        (
            delivered,
            cooldown_rejected,
            delivery_status_counts,
            _merge_conflict_count,
        ) = await bot._select_and_deliver(
            selected,
            prepared_by_tracking_id=prepared_by_tracking_id,
        )
        all_rejected.extend(cooldown_rejected)
        for row in cooldown_rejected:
            bot.telemetry.append_jsonl("rejected.jsonl", row)
            rejected_by_symbol.setdefault(str(row.get("symbol") or "unknown"), []).append(row)

        selected_by_symbol: dict[str, list[Signal]] = {}
        for signal in selected:
            selected_by_symbol.setdefault(signal.symbol, []).append(signal)

        delivered_by_symbol: dict[str, list[Signal]] = {}
        for signal in delivered:
            delivered_by_symbol.setdefault(signal.symbol, []).append(signal)

        delivery_status_counts_by_symbol: dict[str, Counter[str]] = {}
        for row in cooldown_rejected:
            symbol = str(row.get("symbol") or "unknown")
            stage = str(row.get("stage") or "")
            reason = str(row.get("reason") or "")
            if stage == "delivery" and reason.startswith("delivery_"):
                counter = delivery_status_counts_by_symbol.setdefault(symbol, Counter())
                counter[reason.removeprefix("delivery_")] += 1
        success_pool = Counter(
            {
                status: int(delivery_status_counts.get(status, 0) or 0)
                for status in DELIVERY_SUCCESS_STATUSES
            }
        )
        for signal in delivered:
            counter = delivery_status_counts_by_symbol.setdefault(signal.symbol, Counter())
            status = next(
                (item for item in ("sent", "logged") if success_pool.get(item, 0) > 0),
                "sent",
            )
            if success_pool.get(status, 0) > 0:
                success_pool[status] -= 1
            counter[status] += 1
        for res in pipeline_results:
            if isinstance(res.funnel, dict):
                res.funnel["selected"] = len(selected_by_symbol.get(res.symbol, []))
                res.funnel["delivered"] = len(delivered_by_symbol.get(res.symbol, []))
                res.funnel["delivery_status_counts"] = dict(
                    delivery_status_counts_by_symbol.get(res.symbol, Counter())
                )

        now_ts = datetime.now(UTC).isoformat()
        for candidates in all_candidates.values():
            for sig in candidates:
                bot.telemetry.append_jsonl(
                    "candidates.jsonl",
                    {"ts": now_ts, **sig.to_log_row()},
                )
        for sig in delivered:
            bot.telemetry.append_jsonl(
                "selected.jsonl",
                {"ts": now_ts, **sig.to_log_row()},
            )
        for res in pipeline_results:
            bot._emit_cycle_log(
                symbol=res.symbol,
                interval="emergency_fallback",
                event_ts=res.event_ts,
                shortlist_size=len(shortlist),
                tracking_events=[],
                result=res,
                candidates=res.candidates,
                rejected=rejected_by_symbol.get(res.symbol, []),
                delivered=delivered_by_symbol.get(res.symbol, []),
            )

        summary = {
            "shortlist_size": len(shortlist),
            "detector_runs": sum(r.raw_setups for r in pipeline_results),
            "post_filter_candidates": sum(len(r.candidates) for r in pipeline_results),
            "selected_signals": len(selected),
            "delivered_signals": len(delivered),
            "raw_setups": sum(r.raw_setups for r in pipeline_results),
            "candidates": sum(len(r.candidates) for r in pipeline_results),
            "selected": len(selected),
            "delivered": len(delivered),
            "rejected": len(all_rejected),
            "bias": dict(bias_counter),
            "delivery_status_counts": dict(delivery_status_counts),
        }
        bot.last_cycle_summary = summary
        LOG.info("emergency cycle | %s", " ".join(f"{k}={v}" for k, v in summary.items()))
        return summary
