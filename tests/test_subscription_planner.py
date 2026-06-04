"""Unit tests for WS subscription budget planner."""

from __future__ import annotations

from bot.domain.config import BotSettings, WSConfig
from bot.market.subscription_planner import plan_subscription_budget


def test_subscription_budget_caps_depth_and_agg() -> None:
    settings = BotSettings(
        tg_token="test",
        target_chat_id="1",
        ws=WSConfig(
            kline_intervals=("15m",),
            depth_symbol_limit=20,
            max_market_stream_budget=120,
            subscribe_depth=True,
            subscribe_agg_trade=True,
            subscribe_book_ticker=True,
            subscribe_market_streams=True,
        ),
    )
    symbols = [f"SYM{i}USDT" for i in range(50)]
    plan = plan_subscription_budget(symbols, symbols, ws=settings.ws)
    assert plan.kline_streams == 50
    assert plan.total_market <= plan.budget_limit + plan.book_ticker_streams
    assert len(plan.depth_symbols) <= 20
    assert len(plan.agg_trade_symbols) <= len(symbols)
