from __future__ import annotations

from types import SimpleNamespace

import pytest

from bot.models import SymbolMeta
from bot.universe import build_shortlist


def _settings(
    *, quote_asset: str, pinned_symbols: tuple[str, ...] = ()
) -> SimpleNamespace:
    return SimpleNamespace(
        universe=SimpleNamespace(
            quote_asset=quote_asset,
            pinned_symbols=pinned_symbols,
            min_listing_age_days=0,
            min_quote_volume_usd=0.0,
            dynamic_limit=20,
            shortlist_limit=20,
            shortlist_spread_max_bps=15.0,
            shortlist_book_stale_seconds=90.0,
        ),
    )


def _meta(symbol: str, base_asset: str, quote_asset: str) -> SymbolMeta:
    return SymbolMeta(
        symbol=symbol,
        base_asset=base_asset,
        quote_asset=quote_asset,
        contract_type="PERPETUAL",
        status="TRADING",
        onboard_date_ms=0,
    )


@pytest.mark.parametrize(
    ("quote_asset", "symbol", "base_asset"),
    (("USDT", "BTCUSDT", "BTC"), ("USDC", "BTCUSDC", "BTC")),
)
def test_build_shortlist_accepts_usdt_and_usdc_when_quote_matches(
    quote_asset: str,
    symbol: str,
    base_asset: str,
) -> None:
    settings = _settings(quote_asset=quote_asset)
    shortlist, _summary = build_shortlist(
        [_meta(symbol, base_asset, quote_asset)],
        [
            {
                "symbol": symbol,
                "quote_volume": 1_000_000.0,
                "last_price": 100.0,
                "price_change_percent": 1.2,
            }
        ],
        settings,
    )

    assert [row.symbol for row in shortlist] == [symbol]


def test_build_shortlist_filters_by_meta_quote_asset() -> None:
    settings = _settings(quote_asset="USDC")
    shortlist, _summary = build_shortlist(
        [_meta("ETHUSDT", "ETH", "USDT")],
        [
            {
                "symbol": "ETHUSDT",
                "quote_volume": 1_000_000.0,
                "last_price": 2000.0,
                "price_change_percent": 2.0,
            }
        ],
        settings,
    )

    assert shortlist == []


def test_build_shortlist_prefers_fresh_tight_symbol_with_same_liquidity() -> None:
    settings = _settings(quote_asset="USDT")
    shortlist, _summary = build_shortlist(
        [
            _meta("AAAUSDT", "AAA", "USDT"),
            _meta("BBBUSDT", "BBB", "USDT"),
        ],
        [
            {
                "symbol": "AAAUSDT",
                "quote_volume": 20_000_000.0,
                "last_price": 100.0,
                "price_change_percent": 1.5,
                "spread_bps": 1.5,
                "ticker_age_seconds": 4.0,
                "book_age_seconds": 4.0,
                "mark_price_age_seconds": 4.0,
                "oi_change_pct": 6.0,
                "funding_rate": 0.0002,
                "basis_pct": 0.03,
            },
            {
                "symbol": "BBBUSDT",
                "quote_volume": 20_000_000.0,
                "last_price": 100.0,
                "price_change_percent": 1.5,
                "spread_bps": 18.0,
                "ticker_age_seconds": 120.0,
                "book_age_seconds": 120.0,
                "mark_price_age_seconds": 120.0,
                "oi_change_pct": -5.0,
                "funding_rate": 0.0015,
                "basis_pct": 0.24,
            },
        ],
        settings,
        seed_source="ws_light",
    )

    assert [row.symbol for row in shortlist[:2]] == ["AAAUSDT", "BBBUSDT"]
    assert shortlist[0].shortlist_score is not None
    assert shortlist[1].shortlist_score is not None
    assert shortlist[0].shortlist_score > shortlist[1].shortlist_score
    assert shortlist[0].seed_source == "ws_light"
