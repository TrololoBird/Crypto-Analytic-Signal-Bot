"""Structural entry / SL / TP for hunt watch (swing + fib, not naive ATR-only)."""

from __future__ import annotations

from typing import Any


def _f(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def structural_short_levels(
    *,
    price: float,
    impulse_high: float,
    impulse_low: float,
    fib: dict[str, float],
    atr15: float,
    local_support: float,
    local_resistance: float,
) -> dict[str, float | list[float]]:
    """Short fade: SL above structure, TPs toward impulse low / fib retrace."""
    if price <= 0:
        price = impulse_high or 1.0
    atr = max(atr15, price * 0.008)
    ih = max(impulse_high, price, local_resistance)
    il = min(impulse_low, local_support, price) if impulse_low > 0 else local_support

    entry_hi = round(max(price, ih * 0.995), 6)
    entry_lo = round(min(price * 0.998, local_support * 1.002, entry_hi * 0.996), 6)
    stop = round(max(ih * 1.006, entry_hi + atr * 1.1, local_resistance * 1.004), 6)

    tp1 = _f(fib.get("ret_382"))
    tp2 = _f(fib.get("ret_50"))
    if tp1 <= 0 or tp1 >= entry_lo:
        leg = ih - il
        tp1 = round(ih - leg * 0.382, 6) if leg > 0 else round(entry_lo - atr * 2, 6)
    if tp2 <= 0 or tp2 >= tp1:
        leg = ih - il
        tp2 = round(ih - leg * 0.5, 6) if leg > 0 else round(il * 1.01, 6)
    if tp1 >= entry_lo:
        tp1 = round((entry_lo + il) / 2.0, 6)
    if tp2 >= tp1:
        tp2 = round(il * 1.015, 6)

    risk = max(stop - entry_hi, atr * 0.5)
    reward = max(entry_lo - tp1, atr * 0.5)
    rr = round(reward / risk, 2) if risk > 0 else 0.0

    return {
        "entry_zone": [entry_lo, entry_hi],
        "stop_loss": stop,
        "tp1": tp1,
        "tp2": tp2,
        "invalidation_above": stop,
        "risk_reward": rr,
    }


def structural_long_levels(
    *,
    price: float,
    impulse_high: float,
    impulse_low: float,
    fib: dict[str, float],
    atr15: float,
    local_support: float,
    local_resistance: float,
) -> dict[str, float | list[float]]:
    """Long bounce: SL under impulse low, TPs toward resistance / fib ext."""
    if price <= 0:
        price = impulse_low or 1.0
    atr = max(atr15, price * 0.008)
    ih = max(impulse_high, local_resistance, price)
    il = min(impulse_low, local_support, price) if impulse_low > 0 else local_support

    support_zone = _f(fib.get("ret_382"), il)
    entry_lo = round(min(price * 0.998, support_zone * 1.002), 6)
    entry_hi = round(max(price, entry_lo * 1.006), 6)
    stop = round(min(il * 0.992, entry_lo - atr * 1.1, local_support * 0.996), 6)

    tp1 = round(min(local_resistance, ih * 0.998), 6)
    tp2 = _f(fib.get("ext_1272"))
    if tp2 <= tp1:
        leg = ih - il
        tp2 = round(ih + leg * 0.272, 6) if leg > 0 else round(ih * 1.03, 6)
    if tp1 <= entry_hi:
        tp1 = round(entry_hi + atr * 1.5, 6)

    risk = max(entry_lo - stop, atr * 0.5)
    reward = max(tp1 - entry_hi, atr * 0.5)
    rr = round(reward / risk, 2) if risk > 0 else 0.0

    return {
        "entry_zone": [entry_lo, entry_hi],
        "stop_loss": stop,
        "tp1": tp1,
        "tp2": tp2,
        "invalidation_below": stop,
        "risk_reward": rr,
    }
