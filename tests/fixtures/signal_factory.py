"""Factory helpers to create deterministic Signal instances for tests."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from engine.domain.schemas import DEFAULT_SCALE_WEIGHTS, Signal


def safe_usdt_rejected() -> Signal:
    """Minimal-strength SAFE-USDT LONG signal (score=0.15, rejected path)."""
    return Signal(
        symbol="SAFE-USDT",
        setup_id="turtle_soup",
        direction="long",
        score=0.15,
        timeframe="15m",
        entry_low=0.09470,
        entry_high=0.09470,
        stop=0.08920,
        take_profit_1=0.09920,
        take_profit_2=0.14292,
        scale_weights=DEFAULT_SCALE_WEIGHTS,
        reasons=("entry_zone_valid", "adx_ok", "low_score"),
        strategy_family="breakout",
        confirmation_profile="trend_follow",
        created_at=datetime(2026, 6, 25, 12, 0, 0, tzinfo=UTC),
        valid_until=datetime(2026, 6, 25, 16, 0, 0, tzinfo=UTC),
    )


def xag_short_activated() -> Signal:
    """XAG-USDT SHORT activated signal (score=0.52, priority queue)."""
    return Signal(
        symbol="XAG-USDT",
        setup_id="structure_pullback",
        direction="short",
        score=0.52,
        timeframe="15m",
        entry_low=57.2260,
        entry_high=57.6226,
        stop=58.1202,
        take_profit_1=56.7983,
        take_profit_2=56.4100,
        take_profit_3=55.7300,
        scale_weights=DEFAULT_SCALE_WEIGHTS,
        reasons=("entry_zone_valid", "bos_confirmed", "adx_ok", "btc_bias_aligned"),
        strategy_family="reversal",
        confirmation_profile="countertrend",
        bias_4h="down",
        atr_pct=0.85,
        spread_bps=3.2,
        adx_1h=22.0,
        volume_ratio=1.4,
        oi_change_pct=-2.1,
        funding_rate=-0.003,
        mark_price=57.4600,
        btc_bias="bearish",
        created_at=datetime(2026, 6, 25, 12, 0, 0, tzinfo=UTC),
        valid_until=datetime(2026, 6, 25, 16, 0, 0, tzinfo=UTC),
    )
