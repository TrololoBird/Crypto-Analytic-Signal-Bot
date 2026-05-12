from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from bot.application.delivery_orchestrator import DeliveryOrchestrator
from bot.delivery import DeliveredSignal
from bot.domain.schemas import Signal


def _signal(
    *,
    symbol: str,
    setup_id: str,
    direction: str = "long",
    score: float,
    risk_reward: float = 1.5,
) -> Signal:
    return Signal(
        symbol=symbol,
        setup_id=setup_id,
        direction=direction,
        score=score,
        timeframe="15m",
        entry_low=100.0,
        entry_high=100.0,
        stop=98.0 if direction == "long" else 102.0,
        take_profit_1=100.0 + 2.0 * risk_reward
        if direction == "long"
        else 100.0 - 2.0 * risk_reward,
        take_profit_2=106.0 if direction == "long" else 94.0,
    )


def test_select_and_rank_deduplicates_symbol_and_boosts_same_direction_confluence() -> None:
    orchestrator = DeliveryOrchestrator(SimpleNamespace())

    selected = orchestrator.select_and_rank(
        {
            "BTCUSDT": [
                _signal(symbol="BTCUSDT", setup_id="fvg_setup", score=0.60),
                _signal(symbol="BTCUSDT", setup_id="bos_choch", score=0.62),
            ],
            "ETHUSDT": [_signal(symbol="ETHUSDT", setup_id="spread_strategy", score=0.61)],
        },
        max_signals=10,
    )

    assert [signal.symbol for signal in selected] == ["BTCUSDT", "ETHUSDT"]
    assert selected[0].setup_id == "bos_choch"
    assert selected[0].score == 0.635
    assert "confluence_2_setups" in selected[0].reasons
    assert "confluence_setups=bos_choch,fvg_setup" in selected[0].reasons


def test_select_and_rank_keeps_best_direction_when_symbol_conflicts() -> None:
    orchestrator = DeliveryOrchestrator(SimpleNamespace())

    selected = orchestrator.select_and_rank(
        {
            "BTCUSDT": [
                _signal(
                    symbol="BTCUSDT",
                    setup_id="bullish_setup",
                    direction="long",
                    score=0.58,
                ),
                _signal(
                    symbol="BTCUSDT",
                    setup_id="bearish_setup",
                    direction="short",
                    score=0.64,
                ),
            ]
        },
        max_signals=10,
    )

    assert len(selected) == 1
    assert selected[0].symbol == "BTCUSDT"
    assert selected[0].direction == "short"
    assert selected[0].setup_id == "bearish_setup"
    assert selected[0].score == 0.64


@pytest.mark.asyncio
async def test_select_and_deliver_applies_symbol_direction_cooldown() -> None:
    class RepoStub:
        def __init__(self) -> None:
            self.cooldowns: dict[str, datetime] = {}

        async def is_symbol_blacklisted(
            self, symbol: str, *, max_sl_streak: int, pause_hours: int
        ) -> bool:
            return False

        async def get_consecutive_sl(self, symbol: str) -> int:
            return 0

        async def get_active_signals(self, symbol: str | None = None) -> list[dict[str, object]]:
            return []

        async def is_cooldown_active(self, cooldown_key: str, cooldown_minutes: int) -> bool:
            if cooldown_minutes <= 0:
                return False
            last = self.cooldowns.get(cooldown_key)
            if last is None:
                return False
            return (datetime.now(UTC) - last).total_seconds() < cooldown_minutes * 60

        async def set_cooldown(
            self,
            cooldown_key: str,
            sent_at: datetime,
            setup_id: str | None = None,
            symbol: str | None = None,
            cooldown_type: str = "signal_key",
        ) -> None:
            self.cooldowns[cooldown_key] = sent_at

        async def get_market_context(self) -> dict[str, str]:
            return {"btc_bias": "neutral", "eth_bias": "neutral"}

    class DeliveryStub:
        async def deliver(
            self, signals: list[Signal], *, dry_run: bool, btc_bias: str | None = None
        ) -> list[DeliveredSignal]:
            return [
                DeliveredSignal(signal=sig, status="sent", message_id=101, reason=None)
                for sig in signals
            ]

        async def send_analytics_companion(
            self, signal: Signal, *, btc_bias: str | None = None, eth_bias: str | None = None
        ) -> None:
            return None

    class TrackerStub:
        async def set_signal_features_async(
            self, tracking_id: str, features: dict[str, object]
        ) -> None:
            return None

        async def arm_signals_with_messages(
            self,
            signals: list[Signal],
            *,
            dry_run: bool,
            message_ids: dict[str, int | None],
        ) -> None:
            return None

    class AlertsStub:
        async def on_confirmed_signals(
            self, delivered: list[Signal], observed_at: datetime
        ) -> None:
            return None

    async def _wait_noncritical(
        *, label: str, timeout: float, operation: object
    ) -> tuple[bool, object]:
        return True, await operation  # type: ignore[misc]

    bot = SimpleNamespace(
        _modern_repo=RepoStub(),
        settings=SimpleNamespace(
            intelligence=SimpleNamespace(
                max_consecutive_stop_losses=3,
                stop_loss_pause_hours=5,
            ),
            filters=SimpleNamespace(
                cooldown_minutes=60,
                symbol_cooldown_minutes=120,
            ),
        ),
        delivery=DeliveryStub(),
        tracker=TrackerStub(),
        alerts=AlertsStub(),
        _delivery_timeout_seconds=5.0,
        _noncritical_timeout_seconds=2.0,
        _wait_noncritical=_wait_noncritical,
        _sync_ws_tracked_symbols=lambda: None,
    )

    async def _sync_ws_tracked_symbols() -> None:
        return None

    bot._sync_ws_tracked_symbols = _sync_ws_tracked_symbols

    orchestrator = DeliveryOrchestrator(bot)
    signals = [
        _signal(symbol="BTCUSDT", setup_id="spread_strategy", direction="short", score=0.62),
        _signal(symbol="BTCUSDT", setup_id="depth_imbalance", direction="short", score=0.61),
    ]

    delivered, rejected, status_counts = await orchestrator.select_and_deliver(signals)

    assert len(delivered) == 1
    assert status_counts["sent"] == 1
    assert any(row.get("reason") == "symbol_direction_cooldown_active" for row in rejected)
