"""Delivery policy helpers — R-class WATCH-only and benchmark anchors (target spec)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .config import REQUIRED_PINNED_SYMBOLS

if TYPE_CHECKING:
    from .config import BotSettings

# Microstructure / sub-minute setups: no solo ACTION until redesign.
R_CLASS_SETUP_IDS: frozenset[str] = frozenset(
    {
        "price_velocity",
        "whale_walls",
        "spread_strategy",
        "depth_imbalance",
    }
)

BENCHMARK_ANCHOR_SYMBOLS: frozenset[str] = frozenset(REQUIRED_PINNED_SYMBOLS)

METAL_ANCHOR_SYMBOLS: frozenset[str] = frozenset({"XAUUSDT", "XAGUSDT", "PAXGUSDT"})


def is_r_class_setup(setup_id: str) -> bool:
    return str(setup_id or "").strip() in R_CLASS_SETUP_IDS


def is_benchmark_anchor(symbol: str) -> bool:
    """True for pinned benchmark majors (BENCHMARK_ANCHORS.md)."""
    key = str(symbol or "").strip().upper()
    return bool(key) and key in BENCHMARK_ANCHOR_SYMBOLS


def is_metal_anchor(symbol: str) -> bool:
    key = str(symbol or "").strip().upper()
    return bool(key) and key in METAL_ANCHOR_SYMBOLS


def effective_action_min_score(settings: BotSettings, symbol: str) -> float:
    """Higher ACTION bar on benchmark anchors (+delta vs alts)."""
    delivery = settings.delivery
    base = float(delivery.action_min_score)
    if not is_benchmark_anchor(symbol):
        return base
    delta = float(delivery.anchor_action_score_delta)
    if is_metal_anchor(symbol):
        delta += float(delivery.metal_action_score_delta)
    return min(1.0, base + max(0.0, delta))


def r_class_blocks_action(setup_id: str, settings: object) -> bool:
    """Return True when Telegram ACTION must not be sent for this setup."""
    if not is_r_class_setup(setup_id):
        return False
    delivery = getattr(settings, "delivery", None)
    if delivery is None:
        return True
    return bool(getattr(delivery, "r_class_watch_only", True))
