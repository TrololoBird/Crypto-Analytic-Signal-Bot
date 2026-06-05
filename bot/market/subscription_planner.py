"""WebSocket subscription budget planner (shortlist Phase 2).

Caps depth and aggTrade symbol counts so total market streams stay within budget.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bot.domain.config import WSConfig

ORDER_FLOW_ANCHOR_SYMBOLS: tuple[str, ...] = ("btcusdt", "ethusdt")


@dataclass(frozen=True, slots=True)
class SubscriptionBudget:
    """Planned stream counts and symbol caps for one shortlist snapshot."""

    kline_streams: int
    book_ticker_streams: int
    depth_streams: int
    agg_trade_streams: int
    global_streams: int
    total_market: int
    total_public: int
    depth_symbols: tuple[str, ...]
    agg_trade_symbols: tuple[str, ...]
    budget_limit: int


def _normalized_symbols(symbols: list[str]) -> list[str]:
    return list(
        dict.fromkeys(
            str(symbol or "").strip().lower() for symbol in symbols if str(symbol).strip()
        )
    )


def merge_order_flow_tracked_symbols(
    shortlist_symbols: list[str],
    *,
    pending_symbols: list[str] | None = None,
    active_symbols: list[str] | None = None,
    priority_symbols: list[str] | None = None,
) -> list[str]:
    """Merge shortlist + signal symbols; pending/active/radar-hot first for aggTrade budget."""
    pending = _normalized_symbols(pending_symbols or [])
    active = _normalized_symbols(active_symbols or [])
    priority = _normalized_symbols(priority_symbols or [])
    shortlist = _normalized_symbols(shortlist_symbols)
    merged: list[str] = []
    seen: set[str] = set()
    for group in (pending, active, priority, shortlist):
        for symbol in group:
            if symbol not in seen:
                merged.append(symbol)
                seen.add(symbol)
    return merged


def _select_stream_symbols(
    tracked: list[str],
    *,
    cap: int,
    anchors: tuple[str, ...] = ORDER_FLOW_ANCHOR_SYMBOLS,
) -> tuple[str, ...]:
    """Select up to *cap* symbols, always reserving slots for anchor majors."""
    if cap <= 0:
        return ()
    selected: list[str] = []
    seen: set[str] = set()
    for symbol in anchors:
        key = str(symbol or "").strip().lower()
        if not key or key in seen:
            continue
        selected.append(key)
        seen.add(key)
        if len(selected) >= cap:
            return tuple(selected)
    for symbol in tracked:
        key = str(symbol or "").strip().lower()
        if not key or key in seen:
            continue
        selected.append(key)
        seen.add(key)
        if len(selected) >= cap:
            break
    return tuple(selected)


def plan_subscription_budget(
    symbols: list[str],
    tracked_symbols: list[str],
    *,
    ws: WSConfig,
) -> SubscriptionBudget:
    """Compute WS stream plan with depth/aggTrade caps derived from budget."""
    normalized = _normalized_symbols(symbols)
    tracked = _normalized_symbols(tracked_symbols) or normalized
    kline_intervals = tuple(str(item).strip() for item in ws.kline_intervals if str(item).strip())
    kline_streams = len(normalized) * len(kline_intervals)
    book_mode = str(getattr(ws, "book_ticker_stream_mode", "per_symbol") or "per_symbol").lower()
    if ws.subscribe_book_ticker and book_mode == "all":
        book_ticker_streams = 1
    else:
        book_ticker_streams = len(normalized) if ws.subscribe_book_ticker else 0
    global_streams = 0
    if ws.subscribe_market_streams:
        global_streams = 3  # !ticker@arr, !markPrice@arr@1s, !forceOrder@arr

    budget_limit = int(getattr(ws, "max_market_stream_budget", 0) or 0)
    if budget_limit <= 0:
        budget_limit = 300

    reserved = kline_streams + global_streams + 4
    remaining = max(0, budget_limit - reserved)

    configured_depth = int(ws.depth_symbol_limit or 0)
    depth_cap = min(configured_depth, remaining) if configured_depth > 0 else 0
    depth_symbols = _select_stream_symbols(tracked, cap=depth_cap)

    depth_streams = len(depth_symbols) if ws.subscribe_depth and depth_cap > 0 else 0
    remaining_after_depth = max(0, remaining - depth_streams)

    agg_cap = remaining_after_depth if ws.subscribe_agg_trade else 0
    agg_trade_symbols = _select_stream_symbols(tracked, cap=agg_cap) if agg_cap > 0 else ()
    agg_trade_streams = len(agg_trade_symbols)

    total_market = kline_streams + global_streams + agg_trade_streams
    total_public = book_ticker_streams + depth_streams

    return SubscriptionBudget(
        kline_streams=kline_streams,
        book_ticker_streams=book_ticker_streams,
        depth_streams=depth_streams,
        agg_trade_streams=agg_trade_streams,
        global_streams=global_streams,
        total_market=total_market,
        total_public=total_public,
        depth_symbols=depth_symbols,
        agg_trade_symbols=agg_trade_symbols,
        budget_limit=budget_limit,
    )
