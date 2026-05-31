"""Delivery policy helpers — R-class WATCH-only (target spec)."""

from __future__ import annotations

from typing import TYPE_CHECKING

# Microstructure / sub-minute setups: no solo ACTION until redesign.
R_CLASS_SETUP_IDS: frozenset[str] = frozenset(
    {
        "price_velocity",
        "whale_walls",
        "spread_strategy",
        "depth_imbalance",
    }
)


def is_r_class_setup(setup_id: str) -> bool:
    return str(setup_id or "").strip() in R_CLASS_SETUP_IDS


def r_class_blocks_action(setup_id: str, settings: object) -> bool:
    """Return True when Telegram ACTION must not be sent for this setup."""
    if not is_r_class_setup(setup_id):
        return False
    delivery = getattr(settings, "delivery", None)
    if delivery is None:
        return True
    return bool(getattr(delivery, "r_class_watch_only", True))
