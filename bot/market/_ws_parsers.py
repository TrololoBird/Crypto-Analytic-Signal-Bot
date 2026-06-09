"""Private WebSocket message parsers and handlers (extracted from ws.py)."""

from __future__ import annotations

import asyncio
import collections
import logging
import time
from datetime import UTC, datetime
from typing import Any

from bot.domain.events import BookTickerEvent
from bot.domain.schemas import AggTrade

LOG = logging.getLogger("bot.ws_manager")
JsonDict = dict[str, Any]


def should_throttle_ticker_update(manager: Any, symbol: str) -> bool:
    now = time.monotonic()
    last_update = manager._ticker_update_times.get(symbol, 0.0)
    elapsed_ms = (now - last_update) * 1000
    if elapsed_ms < manager._min_ticker_update_interval_ms:
        last_logged = getattr(manager, "_last_ticker_throttle_log", {}).get(symbol, 0.0)
        if now - last_logged >= 30.0:
            if not hasattr(manager, "_last_ticker_throttle_log"):
                manager._last_ticker_throttle_log = {}
            manager._last_ticker_throttle_log[symbol] = now
            LOG.debug(
                "ticker throttled | symbol=%s elapsed=%.0fms min=%.0fms",
                symbol,
                elapsed_ms,
                manager._min_ticker_update_interval_ms,
            )
        return True
    manager._ticker_update_times[symbol] = now
    return False


def should_throttle_mark_price_update(manager: Any, symbol: str) -> bool:
    now = time.monotonic()
    last_update = manager._mark_price_update_times.get(symbol, 0.0)
    elapsed_ms = (now - last_update) * 1000
    if elapsed_ms < 50.0:
        last_logged = getattr(manager, "_last_markprice_throttle_log", {}).get(symbol, 0.0)
        if now - last_logged >= 30.0:
            if not hasattr(manager, "_last_markprice_throttle_log"):
                manager._last_markprice_throttle_log = {}
            manager._last_markprice_throttle_log[symbol] = now
            LOG.debug(
                "mark_price throttled | symbol=%s elapsed=%.0fms min=50ms", symbol, elapsed_ms
            )
        return True
    manager._mark_price_update_times[symbol] = now
    return False


def _ws_kline_to_row(k: JsonDict) -> JsonDict:
    return {
        "time": datetime.fromtimestamp(int(k["t"]) / 1000.0, tz=UTC),
        "open": float(k["o"]),
        "high": float(k["h"]),
        "low": float(k["l"]),
        "close": float(k["c"]),
        "volume": float(k["v"]),
        "close_time": datetime.fromtimestamp(int(k["T"]) / 1000.0, tz=UTC),
        "quote_volume": float(k["q"]),
        "num_trades": int(k["n"]),
        "taker_buy_base_volume": float(k["V"]),
        "taker_buy_quote_volume": float(k["Q"]),
    }


def _clamp(value: float) -> float:
    return max(-1.0, min(1.0, value))


def depth_imbalance_from_book(
    *, bid_qty: float | None, ask_qty: float | None, delta_ratio: float | None
) -> float | None:
    """Return top-of-book depth imbalance, falling back to signed trade flow."""
    if bid_qty is not None and ask_qty is not None and (bid_qty >= 0) and (ask_qty >= 0):
        total = bid_qty + ask_qty
        if total > 0.0:
            return round(_clamp((bid_qty - ask_qty) / total), 4)
    if delta_ratio is None:
        return None
    return round(_clamp(float(delta_ratio)), 4)


def microprice_bias_from_book(
    *,
    bid: float | None,
    ask: float | None,
    bid_qty: float | None = None,
    ask_qty: float | None = None,
    delta_ratio: float | None,
) -> float | None:
    """Return signed microprice bias from L1 book, falling back to trade flow."""
    if bid is None or ask is None or bid <= 0 or (ask <= 0):
        return None
    spread = ask - bid
    mid = (bid + ask) / 2.0
    if mid <= 0 or spread <= 0:
        return None
    if bid_qty is not None and ask_qty is not None and (bid_qty >= 0) and (ask_qty >= 0):
        total_qty = bid_qty + ask_qty
        if total_qty > 0.0:
            microprice = (ask * bid_qty + bid * ask_qty) / total_qty
            half_spread = spread / 2.0
            if half_spread > 0.0:
                return round(_clamp((microprice - mid) / half_spread), 4)
    if delta_ratio is None:
        return None
    return round(_clamp(float(delta_ratio)), 4)


def _parse_depth_levels(raw_levels: Any, *, reverse: bool) -> tuple[tuple[float, float], ...]:
    parsed: list[tuple[float, float]] = []
    if not isinstance(raw_levels, list):
        return ()
    for raw in raw_levels:
        if not isinstance(raw, (list, tuple)) or len(raw) < 2:
            continue
        try:
            price = float(raw[0])
            qty = float(raw[1])
        except TypeError, ValueError:
            continue
        if price <= 0.0 or qty <= 0.0:
            continue
        parsed.append((price, qty))
    parsed.sort(key=lambda item: item[0], reverse=reverse)
    return tuple(parsed)


def _l2_depth_imbalance(manager: Any, symbol: str) -> float | None:
    book = manager._depth_book.get(symbol)
    if not book:
        return None
    bids = book.get("bids") or ()
    asks = book.get("asks") or ()
    if not bids or not asks:
        return None
    best_bid = bids[0][0]
    best_ask = asks[0][0]
    mid = (best_bid + best_ask) / 2.0
    if mid <= 0.0:
        return None
    band_bps = float(getattr(manager._cfg, "depth_band_bps", 8.0) or 8.0)
    band = mid * band_bps / 10000.0
    bid_notional = sum((price * qty for price, qty in bids if mid - price <= band))
    ask_notional = sum((price * qty for price, qty in asks if price - mid <= band))
    if bid_notional <= 0.0 and ask_notional <= 0.0:
        bid_notional = sum((price * qty for price, qty in bids))
        ask_notional = sum((price * qty for price, qty in asks))
    total = bid_notional + ask_notional
    if total <= 0.0:
        return None
    imbalance = (bid_notional - ask_notional) / total
    wall_pressure = manager._depth_wall_pressure.get(symbol)
    if wall_pressure is not None:
        imbalance = imbalance * 0.65 + float(wall_pressure) * 0.35
    return float(round(max(-1.0, min(1.0, imbalance)), 4))


def _update_depth_wall_pressure(
    manager: Any,
    symbol: str,
    bids: tuple[tuple[float, float], ...],
    asks: tuple[tuple[float, float], ...],
    now: float,
) -> None:
    min_notional = float(getattr(manager._cfg, "depth_wall_min_notional", 250000.0) or 0.0)
    if min_notional <= 0.0:
        manager._depth_wall_pressure.pop(symbol, None)
        return
    persistence = float(getattr(manager._cfg, "depth_wall_persistence_seconds", 10.0) or 0.0)
    stale_after = max(5.0, persistence * 2.0)
    state = manager._depth_wall_state.setdefault(symbol, {})
    seen: set[tuple[str, float]] = set()
    bid_wall = ask_wall = 0.0
    for side, levels in (("bid", bids), ("ask", asks)):
        for price, qty in levels:
            notional = price * qty
            if notional < min_notional:
                continue
            key = (side, round(price, 8))
            seen.add(key)
            item = state.get(key)
            if item is None:
                item = {"first_seen": now, "last_seen": now, "max_notional": notional}
                state[key] = item
            else:
                item["last_seen"] = now
                item["max_notional"] = max(float(item.get("max_notional", 0.0)), notional)
            if now - float(item.get("first_seen", now)) >= persistence:
                if side == "bid":
                    bid_wall += notional
                else:
                    ask_wall += notional
    for key, item in list(state.items()):
        if key not in seen and now - float(item.get("last_seen", 0.0)) > stale_after:
            state.pop(key, None)
    total = bid_wall + ask_wall
    if total > 0.0:
        manager._depth_wall_pressure[symbol] = round(
            max(-1.0, min(1.0, (bid_wall - ask_wall) / total)), 4
        )
    else:
        manager._depth_wall_pressure.pop(symbol, None)


def handle_ticker(manager: Any, symbol: str, data: JsonDict) -> None:
    if should_throttle_ticker_update(manager, symbol):
        return
    try:
        manager._ticker_cache[symbol] = {
            "symbol": symbol,
            "last_price": float(data.get("c") or 0.0),
            "quote_volume": float(data.get("q") or 0.0),
            "price_change_percent": float(data.get("P") or 0.0),
            "price_change": float(data.get("p") or 0.0),
            "open_price": float(data.get("o") or 0.0),
            "high_price": float(data.get("h") or 0.0),
            "low_price": float(data.get("l") or 0.0),
            "trade_count": int(float(data.get("n") or 0)),
        }
        manager._ticker_cache_ts = time.monotonic()
    except TypeError, ValueError:
        return


def handle_mini_ticker(manager: Any, symbol: str, data: JsonDict) -> None:
    now = time.monotonic()
    last_full_update = manager._ticker_update_times.get(symbol, 0.0)
    if now - last_full_update < manager._cfg.market_ticker_freshness_seconds:
        return
    if should_throttle_ticker_update(manager, symbol):
        return
    try:
        close_price = float(data.get("c") or 0.0)
        open_price = float(data.get("o") or 0.0)
        price_change_pct = (
            (close_price - open_price) / open_price * 100.0 if open_price > 0 else 0.0
        )
        manager._ticker_cache[symbol] = {
            "symbol": symbol,
            "last_price": close_price,
            "quote_volume": float(data.get("q") or 0.0),
            "price_change_percent": price_change_pct,
            "price_change": close_price - open_price,
            "open_price": open_price,
            "high_price": float(data.get("h") or 0.0),
            "low_price": float(data.get("l") or 0.0),
        }
        manager._ticker_cache_ts = time.monotonic()
    except TypeError, ValueError:
        return


def handle_mark_price(manager: Any, symbol: str, data: JsonDict) -> None:
    if not symbol:
        return
    if should_throttle_mark_price_update(manager, symbol):
        return
    try:
        funding_str = data.get("r")
        funding_rate = (
            float(funding_str) if funding_str is not None and funding_str not in ("", "0") else 0.0
        )
        mark_price = float(data.get("p") or 0.0)
        if mark_price <= 0.0:
            return
        now = time.monotonic()
        manager._mark_price_cache[symbol] = {
            "symbol": symbol,
            "mark_price": mark_price,
            "index_price": float(data.get("i") or 0.0),
            "funding_rate": funding_rate,
            "next_funding_time_ms": int(data.get("T") or 0),
            "updated_at": now,
        }
        manager._mark_price_update_times[symbol] = now
    except TypeError, ValueError:
        return


def handle_force_order(manager: Any, data: JsonDict) -> None:
    try:
        order = data.get("o", {})
        symbol = str(order.get("s") or "").upper()
        side = str(order.get("S") or "").upper()
        qty = float(order.get("q") or 0.0)
        try:
            price = float(order.get("p", 0) or 0)
        except TypeError, ValueError:
            price = 0.0
        ts_ms = int(order.get("T") or data.get("E") or time.time() * 1000)
        if symbol and side in ("BUY", "SELL") and (qty > 0):
            manager._force_order_buffer.append((ts_ms, symbol, side, qty, price))
    except TypeError, ValueError, KeyError:
        return


async def handle_book_ticker(manager: Any, symbol: str, data: JsonDict) -> None:
    if manager._symbols and symbol not in manager._symbols:
        return
    try:
        bid = float(data["b"]) if data.get("b") is not None else None
        ask = float(data["a"]) if data.get("a") is not None else None
        bid_qty = float(data["B"]) if data.get("B") is not None else None
        ask_qty = float(data["A"]) if data.get("A") is not None else None
        event_ts_ms = int(data["E"]) if data.get("E") is not None else None
    except KeyError, TypeError, ValueError:
        return
    async with manager._data_lock:
        manager._book[symbol] = (bid, ask)
        manager._book_qty[symbol] = (bid_qty, ask_qty)
        manager._book_update_times[symbol] = time.monotonic()
    if manager._event_bus is not None:
        manager._event_bus.publish_nowait(
            BookTickerEvent(symbol=symbol, bid=bid, ask=ask, event_ts_ms=event_ts_ms)
        )


async def handle_depth_update(manager: Any, symbol: str, data: JsonDict) -> None:
    if manager._symbols and symbol not in manager._symbols:
        return
    bids = _parse_depth_levels(data.get("b"), reverse=True)
    asks = _parse_depth_levels(data.get("a"), reverse=False)
    if not bids or not asks:
        return
    now = time.monotonic()
    async with manager._data_lock:
        manager._depth_book[symbol] = {"bids": bids, "asks": asks}
        manager._depth_update_times[symbol] = now
        manager._book[symbol] = (bids[0][0], asks[0][0])
        manager._book_qty[symbol] = (bids[0][1], asks[0][1])
        manager._book_update_times[symbol] = now
        _update_depth_wall_pressure(manager, symbol, bids, asks, now)


async def handle_agg_trade(manager: Any, symbol: str, data: JsonDict) -> None:
    if manager._symbols and symbol not in manager._symbols:
        return
    try:
        trade = AggTrade(
            symbol=symbol,
            trade_id=int(data["a"]),
            price=float(data["p"]),
            quantity=float(data["q"]),
            trade_time_ms=int(data["T"]),
            is_buyer_maker=bool(data["m"]),
        )
    except KeyError, TypeError, ValueError:
        return
    pending = manager._pending_agg_trades.setdefault(symbol, [])
    pending.append(trade)
    now = time.monotonic()
    last_flush = manager._last_agg_trade_flush_ts.get(symbol, 0.0)
    flush_interval = float(manager._cfg.agg_trade_flush_interval_ms) / 1000.0
    if now - last_flush < flush_interval and len(pending) < 500:
        return
    batch = pending[:]
    pending.clear()
    manager._last_agg_trade_flush_ts[symbol] = now
    if symbol not in manager._agg_trades:
        manager._agg_trades[symbol] = collections.deque(maxlen=manager._cfg.max_agg_trade_buffer)
    manager._agg_trades[symbol].extend(batch)
    if manager._agg_trade_cbs:
        trade_dt = datetime.fromtimestamp(batch[-1].trade_time_ms / 1000.0, tz=UTC)
        for callback in manager._agg_trade_cbs:
            task = asyncio.create_task(callback(symbol, batch[-1].price, trade_dt))
            manager._attach_task_logging(task, label=f"agg_trade:{symbol}")
