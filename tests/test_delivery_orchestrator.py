from __future__ import annotations

from types import SimpleNamespace

from bot.application.delivery_orchestrator import DeliveryOrchestrator
from bot.models import Signal


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


def test_select_and_rank_deduplicates_symbol_and_boosts_same_direction_confluence() -> (
    None
):
    orchestrator = DeliveryOrchestrator(SimpleNamespace())

    selected = orchestrator.select_and_rank(
        {
            "BTCUSDT": [
                _signal(symbol="BTCUSDT", setup_id="fvg_setup", score=0.60),
                _signal(symbol="BTCUSDT", setup_id="bos_choch", score=0.62),
            ],
            "ETHUSDT": [
                _signal(symbol="ETHUSDT", setup_id="spread_strategy", score=0.61)
            ],
        },
        max_signals=10,
    )

    assert [signal.symbol for signal in selected] == ["BTCUSDT", "ETHUSDT"]
    assert selected[0].setup_id == "bos_choch"
    assert selected[0].score == 0.635
    assert "confluence_2_setups" in selected[0].reasons


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
