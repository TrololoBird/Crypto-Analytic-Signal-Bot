"""Limit-order entry semantics for signal-only delivery and lifecycle tracking.

Manual limit plan: publish entry zone + SL/TP legs. Tracking becomes **active**
when price trades into the zone (realtime aggTrade or bar wick) - like a limit
fill on the exchange. Stop/TP apply only **after** activation. Pending plans
expire by TTL; they are not "cancelled" because price touched SL before fill.
"""

from __future__ import annotations

import math
from typing import Any, Literal

EntryOrderType = Literal["limit"]
DEFAULT_ENTRY_ORDER_TYPE: EntryOrderType = "limit"
DEFAULT_LATE_ENTRY_CHASE_PCT = 0.008

_KNOWN_PROFILES = frozenset(
    {
        "trend_follow",
        "breakout_acceptance",
        "countertrend_exhaustion",
        "divergence_reversal",
    }
)


def resolve_late_entry_chase_pct(settings: Any | None = None) -> float:
    """Resolve chase pct from tracking config (single source) with domain default fallback."""
    if settings is not None:
        tracking = getattr(settings, "tracking", None)
        if tracking is not None:
            raw = getattr(tracking, "late_entry_chase_pct", None)
            if raw is not None:
                try:
                    value = float(raw)
                except TypeError, ValueError:
                    value = math.nan
                if math.isfinite(value) and value > 0.0:
                    return value
    return DEFAULT_LATE_ENTRY_CHASE_PCT


def normalize_confirmation_profile(profile: str | None) -> str:
    value = str(profile or "trend_follow").strip().lower()
    if value in _KNOWN_PROFILES:
        return value
    return "trend_follow"


def bar_intersects_entry_zone(
    *,
    entry_low: float,
    entry_high: float,
    high: float,
    low: float,
) -> bool:
    return low <= entry_high and high >= entry_low


def close_inside_entry_zone(*, close: float, entry_low: float, entry_high: float) -> bool:
    return entry_low <= close <= entry_high


def limit_zone_touched(
    *,
    direction: str,
    entry_low: float,
    entry_high: float,
    high: float,
    low: float,
) -> bool:
    """True when price traded into the limit zone (fill precondition)."""
    if not bar_intersects_entry_zone(
        entry_low=entry_low,
        entry_high=entry_high,
        high=high,
        low=low,
    ):
        return False
    norm = str(direction or "").strip().lower()
    if norm == "long":
        return low <= entry_high
    if norm == "short":
        return high >= entry_low
    return False


def limit_zone_touched_by_price(
    *,
    entry_low: float,
    entry_high: float,
    price: float,
) -> bool:
    return entry_low <= price <= entry_high


def confirm_strategy_activation(
    *,
    direction: str,
    confirmation_profile: str,
    entry_low: float,
    entry_high: float,
    open_: float,
    close: float,
    high: float,
    low: float,
) -> tuple[bool, str]:
    """Legacy bar-quality check - not used for activation anymore (kept for diagnostics)."""
    if not limit_zone_touched(
        direction=direction,
        entry_low=entry_low,
        entry_high=entry_high,
        high=high,
        low=low,
    ):
        return False, "zone_not_touched"
    if not close_inside_entry_zone(close=close, entry_low=entry_low, entry_high=entry_high):
        return False, "close_outside_zone"

    mid = (entry_low + entry_high) / 2.0
    profile = normalize_confirmation_profile(confirmation_profile)
    norm_dir = str(direction or "").strip().lower()

    if profile == "trend_follow":
        ok = close >= open_ if norm_dir == "long" else close <= open_
        return (ok, "trend_bar_confirm" if ok else "trend_bar_reject")

    if profile == "breakout_acceptance":
        ok = close >= mid if norm_dir == "long" else close <= mid
        return (ok, "breakout_accept" if ok else "breakout_reject")

    if profile in {"countertrend_exhaustion", "divergence_reversal"}:
        if norm_dir == "long":
            ok = close > open_ and close >= entry_low
        else:
            ok = close < open_ and close <= entry_high
        return (ok, "reversal_confirm" if ok else "reversal_reject")

    return True, "default_zone_close"


def limit_delivery_ready(
    *,
    direction: str,
    mark_price: float | None,
    entry_low: float,
    entry_high: float,
    stop: float,
    chase_pct: float = DEFAULT_LATE_ENTRY_CHASE_PCT,
) -> tuple[bool, str | None, dict[str, float | str | bool]]:
    """Reject delivery when the limit plan is already invalidated or chasing."""
    details: dict[str, float | str | bool] = {"entry_order_type": DEFAULT_ENTRY_ORDER_TYPE}
    if mark_price is None or not math.isfinite(mark_price) or mark_price <= 0:
        details["mark_price_missing"] = True
        return True, None, details

    chase = max(0.0005, float(chase_pct))
    norm = str(direction or "").strip().lower()
    details["mark_price"] = mark_price

    # At publish time only: reject plans whose SL is already violated by mark.
    # This is not tracking cancellation - the signal never reaches the channel.

    if norm == "long":
        if mark_price <= stop:
            return False, "limit_publish_rejected", details
        if mark_price > entry_high * (1.0 + chase):
            return False, "limit_late_entry_chase", details
    elif norm == "short":
        if mark_price >= stop:
            return False, "limit_publish_rejected", details
        if mark_price < entry_low * (1.0 - chase):
            return False, "limit_late_entry_chase", details

    return True, None, details


def should_activate_limit_entry(
    *,
    direction: str,
    confirmation_profile: str,
    entry_low: float,
    entry_high: float,
    open_: float,
    close: float,
    high: float,
    low: float,
) -> tuple[bool, str]:
    """Pending → active when the limit zone is touched (fill), any bar phase."""
    _ = (confirmation_profile, open_, close)
    if limit_zone_touched(
        direction=direction,
        entry_low=entry_low,
        entry_high=entry_high,
        high=high,
        low=low,
    ):
        return True, "limit_filled"
    return False, "zone_not_touched"


def should_activate_limit_fill_price(
    *,
    entry_low: float,
    entry_high: float,
    price: float,
) -> tuple[bool, str]:
    """Realtime aggTrade path: fill when trade price is inside the zone."""
    if limit_zone_touched_by_price(entry_low=entry_low, entry_high=entry_high, price=price):
        return True, "limit_filled"
    return False, "zone_not_touched"
