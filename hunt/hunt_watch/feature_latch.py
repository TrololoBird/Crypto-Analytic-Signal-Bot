"""Feature-vector and order-book wall latching for signal_history.jsonl.

Snapshots the per-tick market/regime context at signal open, peak MFE, and close
so offline stats and intel dossiers can learn from labelled outcomes.
"""

from __future__ import annotations

from typing import Any

TOP_BOOK_WALL_LEVELS = 5


def feature_vector_from_row(row: dict[str, Any]) -> dict[str, Any]:
    """Compact labelled feature snapshot from a watch tick row."""
    market = row.get("market") or row.get("positioning") or {}
    regime = row.get("regime") or {}
    lifecycle = row.get("lifecycle") or {}
    session = row.get("session") or {}
    return {
        "ts": row.get("ts"),
        "price": row.get("price"),
        "market": dict(market) if isinstance(market, dict) else {},
        "regime": dict(regime) if isinstance(regime, dict) else {},
        "lifecycle_phase": lifecycle.get("phase"),
        "lifecycle_bias": lifecycle.get("recommended_bias"),
        "fall_from_high_pct": lifecycle.get("fall_from_high_pct"),
        "bounce_from_low_pct": lifecycle.get("bounce_from_low_pct"),
        "pos_in_range": session.get("pos_in_range"),
    }


def book_walls_from_depth(
    depth: dict[str, Any] | None,
    *,
    top_n: int = TOP_BOOK_WALL_LEVELS,
) -> dict[str, Any] | None:
    """Top-N bid/ask notional walls from REST depth snapshot."""
    if not isinstance(depth, dict) or depth.get("bid_price") is None:
        return None
    bid_levels = depth.get("bid_levels")
    ask_levels = depth.get("ask_levels")
    walls: dict[str, Any] = {
        "bid_price": depth.get("bid_price"),
        "ask_price": depth.get("ask_price"),
        "bid_levels": bid_levels[:top_n] if isinstance(bid_levels, list) else [],
        "ask_levels": ask_levels[:top_n] if isinstance(ask_levels, list) else [],
    }
    if not walls["bid_levels"] and not walls["ask_levels"]:
        walls["note"] = "aggregated_l1_only"
    return walls


def book_walls_from_row(row: dict[str, Any]) -> dict[str, Any] | None:
    raw = row.get("book_walls")
    return dict(raw) if isinstance(raw, dict) else None
