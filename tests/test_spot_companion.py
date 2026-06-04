"""Unit tests for spot companion metrics."""

from __future__ import annotations

from bot.market.spot_companion import SpotCompanionService


def test_spread_bps_positive_when_futures_premium() -> None:
    spread = SpotCompanionService._spread_bps(100.0, 100.5)
    assert spread is not None
    assert round(spread, 2) == 50.0


def test_lead_return_1m_from_klines() -> None:
    klines = [
        [0, "1", "1", "1", "100", "0", 0, "0", 0, "0", "0", "0"],
        [0, "1", "1", "1", "101", "0", 0, "0", 0, "0", "0", "0"],
    ]
    lead = SpotCompanionService._lead_return_1m(klines)
    assert lead is not None
    assert round(lead, 4) == 1.0
