from __future__ import annotations

from types import SimpleNamespace

import pytest

from bot.domain.schemas import SymbolMeta
from bot.universe import build_shortlist


def _settings(*, quote_asset: str, pinned_symbols: tuple[str, ...] = ()) -> SimpleNamespace:
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


def test_build_shortlist_applies_volume_floor_to_pinned_symbols() -> None:
    settings = _settings(quote_asset="USDT", pinned_symbols=("BTCUSDT",))
    settings.universe.min_quote_volume_usd = 10_000_000.0

    shortlist, _summary = build_shortlist(
        [_meta("BTCUSDT", "BTC", "USDT")],
        [
            {
                "symbol": "BTCUSDT",
                "quote_volume": 1_000_000.0,
                "last_price": 100.0,
                "price_change_percent": 1.2,
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


def test_build_shortlist_fills_bucket_targets_before_strategy_reserve(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup_ids = tuple(f"setup_{idx}" for idx in range(10))
    settings = _settings(quote_asset="USDT")
    settings.universe.dynamic_limit = 12
    settings.universe.shortlist_limit = 6

    def _all_strategy_fits(*_args, **_kwargs) -> tuple[str, ...]:
        return setup_ids

    monkeypatch.setattr("bot.universe._ALL_SETUP_IDS", setup_ids)
    monkeypatch.setattr("bot.universe._strategy_fits_for_row", _all_strategy_fits)

    metas = [
        _meta(f"COIN{idx}USDT", f"COIN{idx}", "USDT")
        for idx in range(settings.universe.dynamic_limit)
    ]
    tickers = [
        {
            "symbol": meta.symbol,
            "quote_volume": 50_000_000.0 - (idx * 1_000_000.0),
            "last_price": 100.0,
            "price_change_percent": (0.5, 4.0, 9.0)[idx % 3],
        }
        for idx, meta in enumerate(metas)
    ]

    shortlist, summary = build_shortlist(metas, tickers, settings)

    assert len(shortlist) == settings.universe.shortlist_limit
    assert summary["trend"] > 0
    assert summary["breakout"] > 0
    assert summary["reversal"] > 0
    assert summary["strategy_seed"] < settings.universe.shortlist_limit


def test_strategy_fits_cover_price_action_setups_on_top_liquid_symbols() -> None:
    settings = _settings(quote_asset="USDT")
    row = {
        "symbol": "COINUSDT",
        "base_asset": "COIN",
        "quote_volume": 75_000_000.0,
        "price_change_percent": 0.6,
        "spread_bps": 2.0,
    }

    from bot.universe import _strategy_fits_for_row

    fits = set(_strategy_fits_for_row(row, settings=settings, liquidity_rank=8))

    assert {
        "bb_squeeze",
        "bos_choch",
        "breaker_block",
        "turtle_soup",
        "stop_hunt_detection",
        "wyckoff_spring",
    }.issubset(fits)
