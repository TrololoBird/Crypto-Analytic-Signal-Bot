"""Resolve freshest executable price for hunt snapshots and Telegram."""
from __future__ import annotations



from typing import Any

from hunt_core.market.streams import HuntCcxtStreams


def resolve_live_price(
    symbol: str,
    *,
    ws_feed: HuntCcxtStreams | None = None,
    book: dict[str, Any] | None = None,
    ws_snap: dict[str, Any] | None = None,
    fallback: float = 0.0,
) -> tuple[float, str]:
    """Best-effort live price: WS ticker → mark → BBO mid → book → fallback."""
    sym = str(symbol).upper()
    fb = float(fallback) if fallback and float(fallback) > 0 else 0.0

    if ws_feed is not None:
        lt = ws_feed.live_ticker(sym)
        if lt:
            last = float(lt.get("last") or 0)
            if last > 0:
                return last, "ws_ticker"

        bbo = ws_feed.live_bbo(sym)
        if bbo:
            bid = float(bbo.get("bid") or 0)
            ask = float(bbo.get("ask") or 0)
            if bid > 0 and ask > 0:
                return (bid + ask) / 2.0, "ws_bbo"
            if bid > 0:
                return bid, "ws_bid"
            if ask > 0:
                return ask, "ws_ask"

        funding = ws_feed.live_funding(sym)
        if funding:
            mark = float(funding.get("markPrice") or 0)
            if mark > 0:
                return mark, "ws_mark"

    snap = ws_snap or (ws_feed.snapshot(sym) if ws_feed is not None else None)
    if snap:
        mark = float(snap.get("live_mark_price") or 0)
        if mark > 0:
            return mark, "ws_snap_mark"

    if book:
        bid = float(book.get("bid_price") or book.get("bid") or 0)
        ask = float(book.get("ask_price") or book.get("ask") or 0)
        if bid > 0 and ask > 0:
            return (bid + ask) / 2.0, "book_mid"
        if bid > 0:
            return bid, "book_bid"
        if ask > 0:
            return ask, "book_ask"

    if fb > 0:
        return fb, "stale_ticker"
    return 0.0, "missing"


def apply_live_price_to_row(
    row: dict[str, Any],
    *,
    ws_feed: HuntCcxtStreams | None = None,
    book: dict[str, Any] | None = None,
) -> float:
    """Overwrite row price with live source; return resolved price."""
    sym = str(row.get("symbol") or "")
    if not sym:
        return 0.0
    market = row.get("market") if isinstance(row.get("market"), dict) else {}
    book_src = book
    if book_src is None and market:
        book_src = {
            "bid_price": market.get("bid"),
            "ask_price": market.get("ask"),
        }
    prev = float(row.get("price") or 0)
    px, source = resolve_live_price(
        sym,
        ws_feed=ws_feed,
        book=book_src,
        fallback=prev,
    )
    if px <= 0:
        return prev
    row["price"] = px
    row["price_source"] = source
    if prev > 0 and abs(px - prev) / prev > 0.0001:
        row["price_stale_delta_pct"] = round((px - prev) / prev * 100.0, 3)
    if isinstance(market, dict):
        market["last_price"] = px
        row["market"] = market
    return px
