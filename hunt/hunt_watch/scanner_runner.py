"""Universe scan — all USD-M tickers → hunt_watchlist.json."""

from __future__ import annotations

import contextlib
import json
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any

import structlog

from engine.domain.config import load_settings

from hunt_core.market import HuntCcxtClient
from hunt_watch.adaptive_thresholds import (
    load_adaptive_store,
    save_adaptive_store,
    update_change_24h,
)
from hunt_watch.paths import WATCHLIST
from hunt_watch.pump_history import (
    _has_recent_leg,
    load_pump_history,
    record_pump_leg,
    save_pump_history,
)
from hunt_watch.screener import (
    HUNT_SCORE_PRIORITY_THRESHOLD,
    HUNT_SCORE_WATCH_THRESHOLD,
    rank_hunt_candidates,
)

LOG = structlog.get_logger("hunt_watch.scanner")


def _enrich_ticker_rows(raw_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    enriched: list[dict[str, Any]] = []
    for row in raw_rows:
        item = dict(row)
        if item.get("high_price") is None and item.get("highPrice") is not None:
            item["high_price"] = item.get("highPrice")
        if item.get("low_price") is None and item.get("lowPrice") is not None:
            item["low_price"] = item.get("lowPrice")
        enriched.append(item)
    return enriched


async def run_scan(
    *, limit: int = 30, min_score: float = HUNT_SCORE_WATCH_THRESHOLD
) -> dict[str, Any]:
    settings = load_settings()
    client = HuntCcxtClient.from_settings(settings)
    await client.load_markets()
    try:
        tickers = _enrich_ticker_rows(await client.fetch_ticker_24h())
        pump_store = load_pump_history()
        adaptive_store = load_adaptive_store()
        stats_map = {sym: st.to_public() for sym, st in pump_store.symbols.items()}
        candidates = rank_hunt_candidates(
            tickers, limit=limit, pump_stats_by_sym=stats_map, adaptive=adaptive_store
        )
        for row in tickers:
            sym = str(row.get("symbol") or "").strip().upper()
            chg = row.get("price_change_percent") or row.get("price_change_pct")
            if sym and chg is not None:
                with contextlib.suppress(TypeError, ValueError):
                    update_change_24h(adaptive_store, sym, float(chg))
        save_adaptive_store(adaptive_store)
        now = datetime.now(UTC)
        for c in candidates:
            if "pump_extreme" not in c.flags and "pump_extreme_z" not in c.flags:
                continue
            if c.change_24h_pct <= 0:
                continue
            if _has_recent_leg(pump_store, c.symbol, "scanner", hours=24.0):
                continue
            record_pump_leg(
                    pump_store,
                    symbol=c.symbol,
                    kind="pump",
                    source="scanner",
                    price=c.last_price,
                    change_24h_pct=c.change_24h_pct,
                    now=now,
                )
        save_pump_history(pump_store)
        watch = [
            c
            for c in candidates
            if c.hunt_score >= min_score
            or ("dump_in_progress" in c.flags and c.hunt_score >= 32.0)
        ]
        priority = [c for c in candidates if c.hunt_score >= HUNT_SCORE_PRIORITY_THRESHOLD]
        pinned = {str(s).upper() for s in settings.universe.pinned_symbols}
        summary: dict[str, Any] = {
            "ts": datetime.now(UTC).isoformat(),
            "ticker_count": len(tickers),
            "candidates": len(candidates),
            "watch_count": len(watch),
            "priority_count": len(priority),
            "min_score": min_score,
            "limit": limit,
            "pinned_overlap": sorted({c.symbol for c in priority if c.symbol in pinned}),
            "watchlist": [
                {
                    **asdict(c),
                    "in_pinned": c.symbol in pinned,
                    "suggest_minute_watch": c.hunt_score >= HUNT_SCORE_PRIORITY_THRESHOLD,
                }
                for c in watch
            ],
        }
        WATCHLIST.parent.mkdir(parents=True, exist_ok=True)
        WATCHLIST.write_text(json.dumps(summary, indent=2), encoding="utf-8")
        LOG.info(
            "hunt_scan_done",
            candidates=len(candidates),
            watch=len(watch),
            priority=len(priority),
            out=str(WATCHLIST),
        )
        for c in priority[:10]:
            LOG.info(
                "hunt_priority",
                symbol=c.symbol,
                score=c.hunt_score,
                bias=c.watch_bias,
                change=c.change_24h_pct,
                flags=",".join(c.flags),
            )
        return summary
    finally:
        await client.close()
