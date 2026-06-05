"""Unit tests for shortlist light-pool funnel (no network)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from bot.domain.config import BotSettings
from bot.domain.schemas import SymbolMeta
from bot.market.universe import build_shortlist, select_light_pool_rows


def _settings(**overrides: object) -> BotSettings:
    base: dict[str, object] = {"tg_token": "test", "target_chat_id": "1"}
    base.update(overrides)
    return BotSettings(**base)


def _meta(symbol: str, *, volume_rank: int = 0) -> SymbolMeta:
    del volume_rank
    old_ms = int((datetime.now(UTC) - timedelta(days=365)).timestamp() * 1000)
    return SymbolMeta(
        symbol=symbol,
        base_asset=symbol.replace("USDT", ""),
        quote_asset="USDT",
        contract_type="PERPETUAL",
        status="TRADING",
        onboard_date_ms=old_ms,
    )


def _ticker(symbol: str, quote_volume: float, price_change_pct: float = 2.0) -> dict[str, float | str]:
    return {
        "symbol": symbol,
        "quote_volume": quote_volume,
        "last_price": 1.0,
        "price_change_percent": price_change_pct,
        "trade_count": 50_000,
    }


def test_select_light_pool_caps_by_volume_and_keeps_pins() -> None:
    settings = _settings()
    pinned = set(settings.universe.pinned_symbols)
    priority = pinned
    rows = [_ticker(f"ALT{i}USDT", float(1_000_000_000 - i * 1_000_000)) for i in range(250)]
    pinned_symbol = settings.universe.pinned_symbols[0]
    rows.append(_ticker(pinned_symbol, 1_000.0))
    gate_passed = [
        {
            "symbol": str(row["symbol"]),
            "quote_volume": float(row["quote_volume"]),
        }
        for row in rows
    ]

    selected, stats = select_light_pool_rows(
        gate_passed,
        settings=settings,
        pinned=pinned,
        priority_symbols=priority,
    )
    assert stats["gate_passed"] == len(gate_passed)
    assert stats["light_pool"] == settings.universe.light_pool_limit
    assert pinned_symbol in {str(row["symbol"]) for row in selected}


def test_build_shortlist_reports_funnel_stages() -> None:
    settings = _settings(
        universe={
            "light_pool_limit": 80,
            "dynamic_limit": 40,
            "shortlist_limit": 15,
            "min_quote_volume_usd": 10_000_000,
            "radar": {"hot_pool_limit": 40, "warm_pool_limit": 80},
        }
    )
    symbols = [f"ALT{i}USDT" for i in range(80)] + list(settings.universe.pinned_symbols)
    meta = [_meta(symbol) for symbol in symbols]
    tickers = [_ticker(symbol, float(100_000_000 - idx * 500_000)) for idx, symbol in enumerate(symbols)]
    shortlist, summary = build_shortlist(meta, tickers, settings, seed_source="unit_test")
    assert summary["gate_passed"] >= summary["light_pool"]
    assert summary["light_pool"] <= settings.universe.light_pool_limit
    assert summary["eligible"] == summary["light_pool"]
    assert len(shortlist) <= settings.universe.shortlist_limit
    assert summary["light_pool_limit"] == settings.universe.light_pool_limit
