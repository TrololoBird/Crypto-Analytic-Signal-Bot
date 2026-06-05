"""Wave E6: liquidation notional, setup_id attribution, schedule labels."""

from __future__ import annotations

import time
from collections import deque

import polars as pl

from bot.domain.labels import normalize_reject_reason, reject_reason_ru
from bot.market.ws import get_liquidation_rollups, get_liquidation_sentiment
from bot.strategies.indicator_divergence import detect_regular_divergence


class _FakeManager:
    def __init__(self) -> None:
        now_ms = int(time.time() * 1000)
        self._force_order_buffer: deque[tuple[int, str, str, float, float]] = deque(
            [
                (now_ms - 1000, "BTCUSDT", "SELL", 2.0, 100_000.0),
                (now_ms - 2000, "BTCUSDT", "BUY", 1.0, 100_000.0),
            ]
        )


def test_liquidation_rollups_use_notional() -> None:
    manager = _FakeManager()
    rollups = get_liquidation_rollups(manager, symbol="BTCUSDT", window_seconds=60)
    assert rollups is not None
    assert rollups["liquidation_long_notional"] == 200_000.0
    assert rollups["liquidation_short_notional"] == 100_000.0
    assert rollups["liquidation_score"] == -100_000.0 / 300_000.0
    assert (
        get_liquidation_sentiment(manager, symbol="BTCUSDT", window_seconds=60)
        == rollups["liquidation_score"]
    )


def test_detect_regular_divergence_respects_setup_id() -> None:
    frame = pl.DataFrame(
        {
            "open": [1.0] * 60,
            "high": [1.05] * 60,
            "low": [i * 0.01 for i in range(60)],
            "close": [1.0] * 60,
            "volume": [1000.0] * 60,
            "atr14": [0.05] * 60,
            "rsi14": [20.0 + (i % 5) for i in range(60)],
        }
    )
    hit = detect_regular_divergence(
        frame,
        setup_id="rsi_divergence_bottom",
        require_oversold=True,
    )
    if hit is not None:
        assert hit.strategy == "rsi_divergence_bottom"


def test_schedule_and_routing_labels() -> None:
    assert normalize_reject_reason("asset_fit.shortlist_not_routed") == "shortlist_not_routed"
    assert "расписания" in reject_reason_ru("runtime.strategy_schedule_inactive")
    assert "lane" in reject_reason_ru("runtime.strategy_lane_excluded").lower()
