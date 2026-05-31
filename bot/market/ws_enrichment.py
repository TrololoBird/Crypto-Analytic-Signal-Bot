from __future__ import annotations


def _clamp(value: float) -> float:
    return max(-1.0, min(1.0, value))


def depth_imbalance_from_book(
    *,
    bid_qty: float | None,
    ask_qty: float | None,
    delta_ratio: float | None,
) -> float | None:
    """Return top-of-book depth imbalance, falling back to signed trade flow."""
    if bid_qty is not None and ask_qty is not None and bid_qty >= 0 and ask_qty >= 0:
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
    if bid is None or ask is None or bid <= 0 or ask <= 0:
        return None
    spread = ask - bid
    mid = (bid + ask) / 2.0
    if mid <= 0 or spread <= 0:
        return None
    if bid_qty is not None and ask_qty is not None and bid_qty >= 0 and ask_qty >= 0:
        total_qty = bid_qty + ask_qty
        if total_qty > 0.0:
            microprice = ((ask * bid_qty) + (bid * ask_qty)) / total_qty
            half_spread = spread / 2.0
            if half_spread > 0.0:
                return round(_clamp((microprice - mid) / half_spread), 4)
    if delta_ratio is None:
        return None
    return round(_clamp(float(delta_ratio)), 4)
