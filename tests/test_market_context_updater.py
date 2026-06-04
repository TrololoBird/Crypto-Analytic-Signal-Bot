"""Unit tests for market context narrative helpers."""

from __future__ import annotations

from types import SimpleNamespace

from bot.runtime.market_context_updater import MarketContextUpdater


def test_fear_greed_proxy_moderate_dump_not_always_zero() -> None:
    regime = SimpleNamespace(
        regime="volatile",
        risk_on_off="neutral",
        funding_sentiment="neutral",
        altcoin_season_index=45.0,
    )
    value, label = MarketContextUpdater._fear_greed_proxy(
        breadth_share=0.45,
        btc_24h_pct=-3.0,
        regime=regime,
        funding_sentiment="neutral",
    )
    assert value > 0
    assert label in {"Fear", "Neutral", "Extreme Fear"}


def test_intraday_vs_24h_note_when_short_term_bounces() -> None:
    note = MarketContextUpdater._intraday_vs_24h_note(
        btc_24h_pct=-5.8,
        tf_1h="1h: легкий восходящий уклон; импульс слабый; тренд слабый",
        tf_15m="15m: восходящий уклон; импульс вверх; тренд выражен; ниже EMA200",
    )
    assert note is not None
    assert "отскакивает" in note


def test_intraday_vs_24h_note_skipped_when_aligned_down() -> None:
    note = MarketContextUpdater._intraday_vs_24h_note(
        btc_24h_pct=-5.8,
        tf_1h="1h: нисходящий уклон; импульс вниз; тренд выражен",
        tf_15m="15m: нисходящий уклон; импульс вниз; тренд выражен",
    )
    assert note is None
