"""Shortlist refresh helpers for SignalBot."""

from __future__ import annotations

import asyncio
import logging
from collections import Counter
from datetime import datetime, timezone
from typing import Any

from ..market_data import BinanceFuturesMarketData
from ..models import UniverseSymbol
from ..universe import build_shortlist

UTC = timezone.utc
LOG = logging.getLogger("bot.application.shortlist_service")

FALLBACK_REASON_WS_CACHE_COLD = "ws_cache_cold"
FALLBACK_REASON_FULL_REFRESH_DUE = "full_refresh_due"
FALLBACK_REASON_REFRESH_EXCEPTION = "refresh_exception"
FALLBACK_REASON_USING_CACHED = "using_cached"
FALLBACK_REASON_USING_PINNED = "using_pinned"
FALLBACK_REASON_LIVE_EMPTY = "live_empty"
SHORTLIST_FALLBACK_REASONS = {
    FALLBACK_REASON_WS_CACHE_COLD: "ws light cache not ready or missing symbol metadata",
    FALLBACK_REASON_FULL_REFRESH_DUE: "full refresh interval reached",
    FALLBACK_REASON_REFRESH_EXCEPTION: "shortlist refresh raised exception",
    FALLBACK_REASON_LIVE_EMPTY: "live refresh returned empty shortlist",
    FALLBACK_REASON_USING_CACHED: "reuse last live shortlist snapshot",
    FALLBACK_REASON_USING_PINNED: "fallback to configured pinned shortlist",
}


def normalize_shortlist_fallback_reason(reason: str | None) -> str | None:
    if reason is None:
        return None
    normalized = str(reason).strip().lower()
    if not normalized:
        return None
    return normalized if normalized in SHORTLIST_FALLBACK_REASONS else "unknown"


class ShortlistService:
    """Encapsulates shortlist build/refresh lifecycle for ``SignalBot``."""

    def __init__(self, bot: Any) -> None:
        self._bot = bot

    @staticmethod
    def _spread_bps(bid: float | None, ask: float | None) -> float | None:
        if bid is None or ask is None or bid <= 0.0 or ask <= 0.0 or ask < bid:
            return None
        mid = (bid + ask) / 2.0
        if mid <= 0.0:
            return None
        return ((ask - bid) / mid) * 10_000.0

    def _record_enrich_error(
        self, *, stage: str, symbol: str, exc: Exception, per_cycle: Counter[str]
    ) -> None:
        bot = self._bot
        stage_key = str(stage).strip().lower() or "unknown"
        symbol_key = str(symbol).strip().upper() or "UNKNOWN"
        exception_type = type(exc).__name__
        reason = str(exc)

        per_cycle[stage_key] += 1

        stage_totals = getattr(bot, "_shortlist_enrich_error_counts", None)
        if not isinstance(stage_totals, Counter):
            stage_totals = Counter()
            bot._shortlist_enrich_error_counts = stage_totals
        stage_totals[stage_key] += 1
        bot._shortlist_enrich_error_total = (
            int(getattr(bot, "_shortlist_enrich_error_total", 0)) + 1
        )

        log_counts = getattr(bot, "_shortlist_enrich_error_log_counts", None)
        if not isinstance(log_counts, Counter):
            log_counts = Counter()
            bot._shortlist_enrich_error_log_counts = log_counts
        log_key = (stage_key, symbol_key, exception_type)
        log_counts[log_key] += 1
        seen = log_counts[log_key]

        if seen == 1 or seen % 50 == 0:
            LOG.warning(
                "shortlist enrich degraded | stage=%s symbol=%s exception_type=%s seen=%d reason=%s",
                stage_key,
                symbol_key,
                exception_type,
                seen,
                reason,
            )

    def _enrich_shortlist_rows(
        self, rows: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        bot = self._bot
        enriched: list[dict[str, Any]] = []
        cycle_errors: Counter[str] = Counter()
        ws = getattr(bot, "_ws_manager", None)
        client = bot.client
        open_interest_cache = (
            getattr(client, "_open_interest_cache", {})
            if isinstance(client, BinanceFuturesMarketData)
            else {}
        )

        for raw in rows:
            row = dict(raw)
            symbol = str(row.get("symbol") or "").strip().upper()
            if not symbol:
                continue

            if ws is not None:
                try:
                    ticker_age = ws.get_ticker_age_seconds(symbol)
                    if ticker_age is not None:
                        row["ticker_age_seconds"] = float(ticker_age)
                except Exception as exc:
                    self._record_enrich_error(
                        stage="ticker_age",
                        symbol=symbol,
                        exc=exc,
                        per_cycle=cycle_errors,
                    )
                try:
                    mark = ws.get_mark_price_snapshot(symbol)
                    mark_age = ws.get_mark_price_age_seconds(symbol)
                    if mark_age is not None:
                        row["mark_price_age_seconds"] = float(mark_age)
                    if mark:
                        funding_rate = mark.get("funding_rate")
                        if funding_rate is not None:
                            row["funding_rate"] = float(funding_rate)
                        mark_price = float(mark.get("mark_price") or 0.0)
                        index_price = float(mark.get("index_price") or 0.0)
                        if mark_price > 0.0 and index_price > 0.0:
                            row["basis_pct"] = (
                                (mark_price - index_price) / index_price
                            ) * 100.0
                except Exception as exc:
                    self._record_enrich_error(
                        stage="mark", symbol=symbol, exc=exc, per_cycle=cycle_errors
                    )
                try:
                    bid, ask = ws.get_book_snapshot(symbol)
                    spread_bps = self._spread_bps(bid, ask)
                    if spread_bps is not None:
                        row["spread_bps"] = spread_bps
                    book_age = ws.get_book_ticker_age_seconds(symbol)
                    if book_age is not None:
                        row["book_age_seconds"] = float(book_age)
                except Exception as exc:
                    self._record_enrich_error(
                        stage="book", symbol=symbol, exc=exc, per_cycle=cycle_errors
                    )

            if isinstance(client, BinanceFuturesMarketData):
                oi_change = client.get_cached_oi_change(symbol)
                if oi_change is not None:
                    row["oi_change_pct"] = float(oi_change)
                ls_ratio = client.get_cached_ls_ratio(symbol)
                if ls_ratio is not None:
                    row["top_account_ls_ratio"] = float(ls_ratio)
                top_position = client.get_cached_top_position_ls_ratio(symbol)
                if top_position is not None:
                    row["top_position_ls_ratio"] = float(top_position)
                global_ratio = client.get_cached_global_ls_ratio(symbol)
                if global_ratio is not None:
                    row["global_account_ls_ratio"] = float(global_ratio)
                if "top_account_ls_ratio" in row and "global_account_ls_ratio" in row:
                    row["top_vs_global_ls_gap"] = float(
                        row["top_account_ls_ratio"]
                    ) - float(row["global_account_ls_ratio"])
                basis_pct = client.get_cached_basis(symbol, period="1h")
                if basis_pct is not None:
                    row["basis_pct"] = float(basis_pct)
                cached_oi = open_interest_cache.get(symbol)
                if cached_oi is not None:
                    _ts, oi_value = cached_oi
                    row["oi_current"] = float(oi_value)
            enriched.append(row)
        bot._shortlist_enrich_last_cycle_errors = dict(cycle_errors)
        return enriched

    async def fetch_symbols_with_retry(self, *, max_retries: int = 1) -> list[Any]:
        for attempt in range(max_retries + 1):
            try:
                return await asyncio.wait_for(
                    self._bot.client.fetch_exchange_symbols(),
                    timeout=10.0,
                )
            except asyncio.TimeoutError:
                LOG.warning(
                    "fetch_exchange_symbols attempt %d/%d timed out",
                    attempt + 1,
                    max_retries + 1,
                )
                if attempt < max_retries:
                    await asyncio.sleep(1.0)
                else:
                    raise
            except Exception as exc:
                LOG.warning(
                    "fetch_exchange_symbols attempt %d/%d failed: %s",
                    attempt + 1,
                    max_retries + 1,
                    exc,
                )
                if attempt < max_retries:
                    await asyncio.sleep(1.0)
                else:
                    raise
        return []

    def extract_symbol_assets(self, symbol: str) -> tuple[str | None, str | None]:
        bot = self._bot
        sym = str(symbol).strip().upper()
        meta = bot._symbol_meta_by_symbol.get(sym)
        if meta is None:
            exchange_cache = getattr(bot.client, "_exchange_info_cache", None)
            if exchange_cache is not None:
                _cached_at, rows = exchange_cache
                cache_map = {
                    str(getattr(row, "symbol", "")).strip().upper(): row for row in rows
                }
                bot._symbol_meta_by_symbol.update(cache_map)
                meta = bot._symbol_meta_by_symbol.get(sym)
        if meta is not None:
            base = str(getattr(meta, "base_asset", "")).strip().upper()
            quote = str(getattr(meta, "quote_asset", "")).strip().upper()
            if base and quote:
                return base, quote

        configured_quote = str(bot.settings.universe.quote_asset).strip().upper()
        if configured_quote and sym.endswith(configured_quote):
            base = sym[: -len(configured_quote)]
            if base:
                return base, configured_quote
        return None, None

    def build_pinned_shortlist(self) -> list[UniverseSymbol]:
        bot = self._bot
        shortlist: list[UniverseSymbol] = []
        for raw_symbol in bot.settings.universe.pinned_symbols:
            symbol = str(raw_symbol).strip().upper()
            base_asset, quote_asset = self.extract_symbol_assets(symbol)
            if not base_asset or not quote_asset:
                LOG.warning(
                    "skipping pinned symbol due to unresolved base/quote assets | symbol=%s configured_quote_asset=%s",
                    symbol,
                    bot.settings.universe.quote_asset,
                )
                continue
            shortlist.append(
                UniverseSymbol(
                    symbol=symbol,
                    base_asset=base_asset,
                    quote_asset=quote_asset,
                    contract_type="PERPETUAL",
                    status="TRADING",
                    onboard_date_ms=0,
                    quote_volume=0.0,
                    price_change_pct=0.0,
                    last_price=0.0,
                    shortlist_bucket="pinned",
                    shortlist_score=1.0,
                    shortlist_reasons=("pinned_symbol",),
                    seed_source="pinned_fallback",
                )
            )
        return shortlist

    async def build_live_shortlist(self) -> tuple[list[UniverseSymbol], dict[str, Any]]:
        bot = self._bot
        timeout_s = max(10.0, float(bot.settings.ws.rest_timeout_seconds) * 2.0)
        symbol_meta_list, tickers_24h = await asyncio.wait_for(
            asyncio.gather(
                self.fetch_symbols_with_retry(max_retries=1),
                bot.client.fetch_ticker_24h(),
            ),
            timeout=timeout_s,
        )
        bot._symbol_meta_by_symbol = {
            str(getattr(row, "symbol", "")).strip().upper(): row
            for row in symbol_meta_list
        }
        shortlist, summary = build_shortlist(
            symbol_meta_list,
            self._enrich_shortlist_rows(list(tickers_24h)),
            bot.settings,
            seed_source="rest_full",
        )
        return shortlist, summary

    async def build_light_shortlist(
        self,
    ) -> tuple[list[UniverseSymbol], dict[str, Any]]:
        bot = self._bot
        ws = getattr(bot, "_ws_manager", None)
        if (
            ws is None
            or not bot._symbol_meta_by_symbol
            or not ws.is_ticker_cache_warm()
        ):
            return [], {
                "mode": "ws_light",
                "eligible": 0,
                "dynamic_pool": 0,
                "pinned": 0,
            }

        tickers = self._enrich_shortlist_rows(ws.get_global_ticker_data())
        shortlist, summary = build_shortlist(
            list(bot._symbol_meta_by_symbol.values()),
            tickers,
            bot.settings,
            seed_source="ws_light",
        )
        return shortlist, summary

    async def do_refresh_shortlist(self) -> list[UniverseSymbol]:
        bot = self._bot
        LOG.info("refreshing shortlist...")

        source = "pinned_fallback"
        source_before = str(getattr(bot, "_shortlist_source", "startup"))
        summary: dict[str, Any] = {}
        shortlist = self.build_pinned_shortlist()
        now = datetime.now(UTC)
        fallback_reason: str | None = FALLBACK_REASON_USING_PINNED
        cached_shortlist_age_s: float | None = None
        cached_shortlist_size: int | None = None
        full_interval = int(
            getattr(
                bot.settings.universe,
                "full_refresh_interval_seconds",
                bot.settings.runtime.shortlist_refresh_interval_seconds,
            )
        )
        last_full = getattr(bot, "_last_shortlist_full_refresh_at", None)
        full_refresh_due = (
            last_full is None or (now - last_full).total_seconds() >= full_interval
        )
        ws = getattr(bot, "_ws_manager", None)
        ws_cache_warm = bool(
            ws is not None
            and hasattr(ws, "is_ticker_cache_warm")
            and ws.is_ticker_cache_warm()
        )
        has_symbol_meta = bool(getattr(bot, "_symbol_meta_by_symbol", {}))
        LOG.info(
            "shortlist refresh decision | full_refresh_due=%s ws_cache_warm=%s has_symbol_meta=%s",
            full_refresh_due,
            ws_cache_warm,
            has_symbol_meta,
        )

        try:
            live_shortlist: list[UniverseSymbol] = []
            live_summary: dict[str, Any] = {}
            if not full_refresh_due:
                live_shortlist, live_summary = await self.build_light_shortlist()
                if live_shortlist:
                    source = "ws_light"
                    fallback_reason = None
                elif not ws_cache_warm or not has_symbol_meta:
                    fallback_reason = FALLBACK_REASON_WS_CACHE_COLD
            if not live_shortlist:
                if full_refresh_due:
                    fallback_reason = FALLBACK_REASON_FULL_REFRESH_DUE
                live_shortlist, live_summary = await self.build_live_shortlist()
                if live_shortlist:
                    bot._last_shortlist_full_refresh_at = now
                    source = "rest_full"
                    fallback_reason = fallback_reason if fallback_reason else None
            if live_shortlist:
                shortlist = live_shortlist
                summary = live_summary
                bot._last_live_shortlist = list(live_shortlist)
                bot._last_live_shortlist_at = now
            else:
                fallback_reason = fallback_reason or FALLBACK_REASON_LIVE_EMPTY
            if not live_shortlist and bot._last_live_shortlist:
                shortlist = list(bot._last_live_shortlist)
                source = "cached"
                fallback_reason = FALLBACK_REASON_USING_CACHED
        except Exception as exc:
            fallback_reason = FALLBACK_REASON_REFRESH_EXCEPTION
            if bot._last_live_shortlist:
                shortlist = list(bot._last_live_shortlist)
                source = "cached"
                LOG.warning("shortlist refresh failed, using cached shortlist: %s", exc)
            else:
                fallback_reason = FALLBACK_REASON_USING_PINNED
                LOG.warning("shortlist refresh failed, using pinned fallback: %s", exc)

        if source == "cached":
            last_live_at = getattr(bot, "_last_live_shortlist_at", None)
            if isinstance(last_live_at, datetime):
                cached_shortlist_age_s = max(0.0, (now - last_live_at).total_seconds())
            cached_shortlist_size = len(getattr(bot, "_last_live_shortlist", []))

        fallback_reason = normalize_shortlist_fallback_reason(fallback_reason)

        async with bot._shortlist_lock:
            bot._shortlist = shortlist
        bot._shortlist_source = source

        top_scores = [
            {
                "symbol": item.symbol,
                "score": item.shortlist_score,
                "reasons": list(item.shortlist_reasons[:3]),
                "seed_source": item.seed_source,
            }
            for item in shortlist[:5]
        ]
        telemetry_manager = (
            bot._get_telemetry_manager()
            if hasattr(bot, "_get_telemetry_manager")
            else None
        )
        if telemetry_manager is not None:
            telemetry_manager.emit_shortlist_refresh(
                source=source,
                source_before=source_before,
                fallback_reason=fallback_reason,
                cached_shortlist_age_s=cached_shortlist_age_s,
                cached_shortlist_size=cached_shortlist_size,
                shortlist_size=len(shortlist),
                shortlist_symbols=[item.symbol for item in shortlist[:20]],
                top_scores=top_scores,
                summary=summary,
            )
        else:
            bot.telemetry.append_jsonl(
                "shortlist.jsonl",
                {
                    "ts": datetime.now(UTC).isoformat(),
                    "source": source,
                    "source_before": source_before,
                    "source_after": source,
                    "fallback_reason": fallback_reason,
                    "cached_shortlist_age_s": cached_shortlist_age_s,
                    "cached_shortlist_size": cached_shortlist_size,
                    "size": len(shortlist),
                    "symbols": [item.symbol for item in shortlist[:20]],
                    "eligible": summary.get("eligible"),
                    "dynamic_pool": summary.get("dynamic_pool"),
                    "pinned": summary.get("pinned"),
                    "mode": summary.get("mode", source),
                    "avg_score": summary.get("avg_score"),
                    "enrich_errors_by_stage": dict(
                        getattr(bot, "_shortlist_enrich_error_counts", {})
                    ),
                    "enrich_errors_total": int(
                        getattr(bot, "_shortlist_enrich_error_total", 0)
                    ),
                    "enrich_errors_last_cycle": dict(
                        getattr(bot, "_shortlist_enrich_last_cycle_errors", {})
                    ),
                    "top_scores": top_scores,
                },
            )

        LOG.info(
            "shortlist refresh complete | source=%s mode=%s fallback_reason=%s size=%d eligible=%s dynamic_pool=%s pinned=%s avg_score=%s",
            source,
            summary.get("mode", source),
            fallback_reason,
            len(shortlist),
            summary.get("eligible"),
            summary.get("dynamic_pool"),
            summary.get("pinned"),
            summary.get("avg_score"),
        )
        return shortlist

    async def refresh_shortlist_periodic(self) -> None:
        bot = self._bot
        await asyncio.sleep(5)
        while not bot._shutdown.is_set():
            await self.do_refresh_shortlist()
            try:
                await asyncio.wait_for(
                    bot._shutdown.wait(),
                    timeout=max(
                        15,
                        int(
                            getattr(
                                bot.settings.universe,
                                "light_refresh_interval_seconds",
                                75,
                            )
                        ),
                    ),
                )
            except asyncio.TimeoutError:
                continue
