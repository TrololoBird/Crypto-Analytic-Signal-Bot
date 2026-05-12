from __future__ import annotations

from datetime import UTC, datetime

import polars as pl
import pytest

from bot.application.shortlist_service import ShortlistService
from bot.domain.config import BotSettings
from bot.features_microstructure import add_microstructure_features
from bot.domain.schemas import PreparedSymbol, Signal, UniverseSymbol
from bot.setup_base import BaseSetup, SetupParams
from bot.telegram import sender as telegram_sender


def _prepared() -> PreparedSymbol:
    frame = pl.DataFrame(
        {
            "time": [datetime(2026, 1, 1, tzinfo=UTC)],
            "open": [100.0],
            "high": [101.0],
            "low": [99.0],
            "close": [100.0],
            "volume": [1000.0],
        }
    )
    return PreparedSymbol(
        universe=UniverseSymbol(
            symbol="BTCUSDT",
            base_asset="BTC",
            quote_asset="USDT",
            contract_type="PERPETUAL",
            status="TRADING",
            onboard_date_ms=0,
            quote_volume=1_000_000.0,
            price_change_pct=0.0,
            last_price=100.0,
        ),
        work_1h=frame,
        work_15m=frame,
        work_4h=frame,
        bid_price=99.9,
        ask_price=100.1,
        spread_bps=2.0,
    )


class _ExplodingSetup(BaseSetup):
    setup_id = "exploding_setup"

    def detect(self, prepared: PreparedSymbol, settings: BotSettings) -> Signal | None:
        raise RuntimeError("boom")

    def get_optimizable_params(self, settings: BotSettings | None = None) -> dict[str, float]:
        return {}


def test_base_setup_converts_strategy_exception_to_error_result() -> None:
    settings = BotSettings(tg_token="1" * 30, target_chat_id="123")
    result = _ExplodingSetup(SetupParams(), settings).calculate(_prepared())

    assert result.signal is None
    assert result.error == "boom"
    assert result.decision.reason_code == "bug.error"
    assert result.decision.details["error_class"] == "bug"
    assert result.decision.details["exception_type"] == "RuntimeError"


def test_microstructure_clips_delta_ratio_before_signed_flow() -> None:
    frame = pl.DataFrame({"delta_ratio": [-2.0, 0.25, 2.0, None]})
    result = add_microstructure_features(frame)

    assert result["signed_order_flow"].to_list() == [-1.0, -0.5, 1.0, 0.0]


def test_premium_index_merge_does_not_overwrite_existing_ws_funding() -> None:
    rows = [{"symbol": "BTCUSDT", "funding_rate": 0.0002, "basis_pct": None}]
    premium = {"BTCUSDT": {"funding_rate": 0.0009, "basis_pct": 0.03}}

    merged = ShortlistService._merge_premium_index_rows(rows, premium)

    assert merged[0]["funding_rate"] == 0.0002
    assert merged[0]["basis_pct"] == 0.03


@pytest.mark.asyncio
async def test_telegram_sender_rejects_empty_token_before_bot_creation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_if_called(*_args, **_kwargs):
        raise AssertionError("Bot constructor must not be called for an empty token")

    monkeypatch.setattr(telegram_sender, "Bot", fail_if_called)
    sender = telegram_sender.TelegramSender("", "123")

    with pytest.raises(ValueError, match="bot token"):
        await sender.initialize()
