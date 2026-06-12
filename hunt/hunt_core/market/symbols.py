"""Binance USD-M symbol id ↔ CCXT unified symbol."""

from __future__ import annotations


def to_binance_symbol(symbol: str) -> str:
    return str(symbol or "").strip().upper()


def to_ccxt_symbol(symbol: str, *, markets: dict | None = None) -> str:
    """Map BTCUSDT → BTC/USDT:USDT (linear perp)."""
    sym = to_binance_symbol(symbol)
    if not sym:
        return sym
    if "/" in sym:
        return sym
    if markets and sym in markets:
        return str(markets[sym].get("symbol") or sym)
    if sym.endswith("USDT"):
        base = sym[:-4]
        return f"{base}/USDT:USDT"
    return sym


def from_ccxt_symbol(symbol: str) -> str:
    """Map BTC/USDT:USDT → BTCUSDT."""
    raw = str(symbol or "").strip()
    if not raw:
        return raw
    if ":" in raw:
        raw = raw.split(":", 1)[0]
    return raw.replace("/", "").upper()
