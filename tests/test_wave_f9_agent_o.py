"""Wave F9 agent O: prescore basis warm, rerank outcome penalties, enrichment batch pace."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bot.domain.config import BotSettings
from bot.domain.schemas import SymbolMeta, UniverseSymbol
from bot.market.enrichment import PublicIntelligenceService
from bot.market.rate_limit import REST_WEIGHT_SOFT_LIMIT
from bot.market.universe import (
    build_shortlist,
    rerank_shortlist,
    warm_prescore_basis_rest,
    warm_prescore_basis_rows,
)
from bot.runtime.shortlist_service import ShortlistService


def _settings(**overrides: object) -> BotSettings:
    base: dict[str, object] = {"tg_token": "test", "target_chat_id": "1"}
    base.update(overrides)
    return BotSettings(**base)


def _meta(symbol: str) -> SymbolMeta:
    old_ms = int((datetime.now(UTC) - timedelta(days=365)).timestamp() * 1000)
    return SymbolMeta(
        symbol=symbol,
        base_asset=symbol.replace("USDT", ""),
        quote_asset="USDT",
        contract_type="PERPETUAL",
        status="TRADING",
        onboard_date_ms=old_ms,
    )


def _ticker(
    symbol: str, quote_volume: float, *, basis_pct: float | None = None
) -> dict[str, float | str]:
    row: dict[str, float | str] = {
        "symbol": symbol,
        "quote_volume": quote_volume,
        "last_price": 100.0,
        "price_change_percent": 2.5,
        "trade_count": 50_000,
    }
    if basis_pct is not None:
        row["basis_pct"] = basis_pct
    return row


def _universe_row(symbol: str, *, quote_volume: float = 100_000_000.0) -> UniverseSymbol:
    return UniverseSymbol(
        symbol=symbol,
        base_asset=symbol.replace("USDT", ""),
        quote_asset="USDT",
        contract_type="PERPETUAL",
        status="TRADING",
        onboard_date_ms=int((datetime.now(UTC) - timedelta(days=365)).timestamp() * 1000),
        quote_volume=quote_volume,
        price_change_pct=2.5,
        last_price=100.0,
        trade_count_24h=50_000,
        shortlist_bucket="trend",
        shortlist_score=0.8,
        shortlist_reasons=("liquidity",),
        seed_source="unit_test",
        liquidity_rank=1,
        strategy_fits=("order_block",),
    )


def test_warm_prescore_basis_rows_fills_from_ws_and_cache() -> None:
    rows = [
        {"symbol": "AAAUSDT", "quote_volume": 200_000_000.0},
        {"symbol": "BBBUSDT", "quote_volume": 150_000_000.0},
    ]
    settings = _settings()

    stats = warm_prescore_basis_rows(
        rows,
        settings=settings,
        limit=2,
        get_mark_basis=lambda symbol: 0.12 if symbol == "AAAUSDT" else None,
        get_cached_basis=lambda symbol: 0.08 if symbol == "BBBUSDT" else None,
    )

    assert rows[0]["basis_pct"] == pytest.approx(0.12)
    assert rows[1]["basis_pct"] == pytest.approx(0.08)
    assert stats["basis_warm_candidates"] == 2
    assert stats["basis_warm_ws_filled"] == 1
    assert stats["basis_warm_cache_filled"] == 1
    assert stats["basis_warm_still_missing"] == 0


@pytest.mark.asyncio
async def test_warm_prescore_basis_rest_bounded_fetch() -> None:
    rows = [{"symbol": "ETHUSDT", "quote_volume": 500_000_000.0}]
    fetch = AsyncMock(return_value=0.05)
    stats = await warm_prescore_basis_rest(
        rows,
        fetch,
        settings=_settings(),
        limit=1,
    )
    fetch.assert_awaited_once_with("ETHUSDT")
    assert rows[0]["basis_pct"] == pytest.approx(0.05)
    assert stats["basis_warm_attempted"] == 1
    assert stats["basis_warm_ok"] == 1


def test_build_shortlist_reports_basis_warm_summary() -> None:
    settings = _settings(
        universe={
            "light_pool_limit": 50,
            "dynamic_limit": 40,
            "shortlist_limit": 15,
            "min_quote_volume_usd": 10_000_000,
            "radar": {"enabled": True, "hot_pool_limit": 40, "warm_pool_limit": 50},
        }
    )
    symbols = [f"ALT{i}USDT" for i in range(30)] + list(settings.universe.pinned_symbols)
    meta = [_meta(symbol) for symbol in symbols]
    tickers = [
        _ticker(symbol, float(100_000_000 - idx * 500_000)) for idx, symbol in enumerate(symbols)
    ]

    _, summary = build_shortlist(
        meta,
        tickers,
        settings,
        seed_source="unit_test",
        get_mark_basis=lambda symbol: 0.1 if symbol == "ALT0USDT" else None,
    )

    assert "basis_warm" in summary
    assert summary["basis_warm"]["basis_warm_candidates"] >= 1


def test_rerank_shortlist_applies_outcome_penalties() -> None:
    settings = _settings()
    current = [
        _universe_row("AAAUSDT", quote_volume=200_000_000.0),
        _universe_row("BBBUSDT", quote_volume=180_000_000.0),
    ]
    tickers = [
        {
            "symbol": "AAAUSDT",
            "quote_volume": 200_000_000.0,
            "price_change_percent": 2.5,
            "last_price": 100.0,
        },
        {
            "symbol": "BBBUSDT",
            "quote_volume": 180_000_000.0,
            "price_change_percent": 2.5,
            "last_price": 100.0,
        },
    ]

    baseline = rerank_shortlist(current, tickers, settings)
    penalized = rerank_shortlist(
        current,
        tickers,
        settings,
        outcome_penalties={"AAAUSDT": 0.25},
    )

    baseline_aaa = next(item for item in baseline if item.symbol == "AAAUSDT")
    penalized_aaa = next(item for item in penalized if item.symbol == "AAAUSDT")
    assert (penalized_aaa.shortlist_score or 0.0) < (baseline_aaa.shortlist_score or 0.0)
    assert any("outcome_derank" in reason for reason in (penalized_aaa.shortlist_reasons or ()))


@pytest.mark.asyncio
async def test_do_rerank_shortlist_passes_outcome_penalties() -> None:
    bot = SimpleNamespace(
        settings=_settings(),
        _shortlist=[_universe_row("BTCUSDT")],
        _shortlist_lock=asyncio.Lock(),
        _modern_repo=None,
        client=MagicMock(),
    )

    ws = MagicMock()
    ws.is_ticker_cache_warm.return_value = True
    ws.get_global_ticker_data.return_value = [
        {
            "symbol": "BTCUSDT",
            "quote_volume": 1_000_000_000.0,
            "price_change_percent": 2.0,
            "last_price": 100_000.0,
        }
    ]
    bot._ws_manager = ws

    service = ShortlistService(bot)
    service._enrich_shortlist_rows = MagicMock(  # type: ignore[method-assign]
        return_value=ws.get_global_ticker_data.return_value
    )

    with (
        patch(
            "bot.runtime.shortlist_service.rerank_shortlist",
            return_value=[_universe_row("BTCUSDT")],
        ) as rerank_mock,
        patch(
            "bot.runtime.shortlist_service._outcome_derank_penalties",
            new=AsyncMock(return_value={"BTCUSDT": 0.12}),
        ),
    ):
        await service.do_rerank_shortlist()

    assert rerank_mock.call_count == 1
    assert rerank_mock.call_args.kwargs["outcome_penalties"] == {"BTCUSDT": 0.12}


@pytest.mark.asyncio
async def test_enrichment_batch_pace_waits_on_high_weight() -> None:
    settings = _settings()
    budget = MagicMock()
    budget.used_weight = REST_WEIGHT_SOFT_LIMIT + 50
    client = MagicMock()
    client._weight_budget = budget

    service = PublicIntelligenceService(settings, client, MagicMock())
    sleep_mock = AsyncMock()

    with patch("bot.market.enrichment.asyncio.sleep", new=sleep_mock):
        await service._pace_enrichment_batch(label="oi", batch_size=10)

    sleep_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_derivatives_refresh_uses_fetch_value_batch() -> None:
    settings = _settings()
    client = MagicMock()
    client.get_cached_oi_change.return_value = None
    client.get_cached_ls_ratio.return_value = None
    client.get_cached_funding_rate.return_value = 0.0001
    client.get_cached_open_interest.return_value = 1_000.0
    client.get_cached_global_ls_ratio.return_value = 1.0
    client.get_cached_taker_ratio.return_value = 1.0
    client.get_cached_basis.return_value = 0.05
    client.get_cached_funding_trend.return_value = "flat"

    service = PublicIntelligenceService(settings, client, MagicMock())
    batch_mock = AsyncMock(return_value={})
    service._fetch_value_batch = batch_mock  # type: ignore[method-assign]

    await service._build_derivatives_snapshot(["BTCUSDT"])

    assert batch_mock.await_count == 2
    first_call_symbols = batch_mock.await_args_list[0].args[0]
    assert first_call_symbols == ["BTCUSDT"]
