from __future__ import annotations

from bot.delivery import format_signal_text
from bot.domain.schemas import Signal


def test_signal_text_uses_structured_trader_sections() -> None:
    signal = Signal(
        symbol="BTCUSDT",
        setup_id="bb_squeeze",
        direction="long",
        score=0.72,
        timeframe="15m",
        entry_low=100.0,
        entry_high=101.0,
        stop=98.0,
        take_profit_1=104.0,
        take_profit_2=106.0,
        reasons=("bb_squeeze_long", "bb_width=3.00"),
    )

    text = format_signal_text(signal, pending_expiry_minutes=60)

    assert "<b>Setup</b>" in text
    assert "<b>Risk</b>" in text
    assert "<b>Entry zone</b>" in text
    assert "<b>Targets</b>" in text
    assert "<b>Invalidation</b>" in text
    assert "<b>Score</b> <code>72%</code>" in text
