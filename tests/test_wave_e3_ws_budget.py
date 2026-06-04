"""Wave E3: shortlist order-flow WS budget and enrichment."""

from __future__ import annotations

from unittest.mock import MagicMock

from bot.domain.config import BotSettings, WSConfig
from bot.domain.schemas import AggTradeSnapshot
from bot.market.subscription_planner import (
    ORDER_FLOW_ANCHOR_SYMBOLS,
    merge_order_flow_tracked_symbols,
    plan_subscription_budget,
)
from bot.runtime.shortlist_service import ShortlistService


def test_merge_order_flow_tracked_symbols_pending_before_shortlist() -> None:
    shortlist = [f"SYM{i}USDT" for i in range(5)]
    pending = ["PENDUSDT"]
    active = ["ACTUSDT"]
    merged = merge_order_flow_tracked_symbols(
        shortlist,
        pending_symbols=pending,
        active_symbols=active,
    )
    assert merged[:2] == ["pendusdt", "actusdt"]
    assert all(sym in merged for sym in (s.lower() for s in shortlist))
    assert len(merged) == len(set(merged))
    assert len(merged) == 7


def test_plan_subscription_budget_always_includes_anchors() -> None:
    settings = BotSettings(
        tg_token="test",
        target_chat_id="1",
        ws=WSConfig(
            kline_intervals=("15m",),
            depth_symbol_limit=2,
            max_market_stream_budget=120,
            subscribe_depth=True,
            subscribe_agg_trade=True,
            subscribe_book_ticker=False,
            subscribe_market_streams=True,
        ),
    )
    # Fill tracked with non-anchor symbols; cap is tight.
    tracked = [f"sym{i}usdt" for i in range(20)]
    plan = plan_subscription_budget([], tracked, ws=settings.ws)
    assert plan.depth_symbols == ORDER_FLOW_ANCHOR_SYMBOLS[:2]
    assert plan.agg_trade_symbols[:2] == ORDER_FLOW_ANCHOR_SYMBOLS[:2]


def test_plan_subscription_budget_pending_symbols_preferred_over_shortlist_tail() -> None:
    settings = BotSettings(
        tg_token="test",
        target_chat_id="1",
        ws=WSConfig(
            kline_intervals=("15m",),
            depth_symbol_limit=4,
            max_market_stream_budget=120,
            subscribe_depth=True,
            subscribe_agg_trade=True,
            subscribe_book_ticker=False,
            subscribe_market_streams=True,
        ),
    )
    shortlist = [f"sym{i}usdt" for i in range(10)]
    pending = ["pendusdt"]
    tracked = merge_order_flow_tracked_symbols(shortlist, pending_symbols=pending)
    plan = plan_subscription_budget(shortlist, tracked, ws=settings.ws)
    assert "pendusdt" in plan.depth_symbols
    assert "btcusdt" in plan.depth_symbols
    assert "ethusdt" in plan.depth_symbols


def test_enrich_shortlist_rows_adds_order_flow_fields() -> None:
    bot = MagicMock()
    ws = MagicMock()
    bot._ws_manager = ws
    bot.client = MagicMock()
    ws.get_agg_trade_snapshot.return_value = AggTradeSnapshot(
        symbol="BTCUSDT",
        trade_count=12,
        buy_qty=100.0,
        sell_qty=80.0,
        delta_ratio=0.1111,
    )
    ws.get_depth_imbalance.return_value = 0.42
    ws.get_microprice_bias.return_value = -0.15
    ws.get_ticker_age_seconds.return_value = None
    ws.get_mark_price_snapshot.return_value = None
    ws.get_mark_price_age_seconds.return_value = None
    ws.get_book_snapshot.return_value = (None, None)
    ws.get_book_ticker_age_seconds.return_value = None
    ws.get_liquidation_sentiment.return_value = None

    service = ShortlistService(bot)
    rows = service._enrich_shortlist_rows([{"symbol": "BTCUSDT"}])

    assert len(rows) == 1
    assert rows[0]["agg_trade_delta_30s"] == 0.1111
    assert rows[0]["depth_imbalance"] == 0.42
    assert rows[0]["microprice_bias"] == -0.15
    ws.get_agg_trade_snapshot.assert_called_once_with("BTCUSDT", window_seconds=30)
