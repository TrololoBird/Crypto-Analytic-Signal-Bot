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


def test_signal_text_has_explicit_confluence_section() -> None:
    signal = Signal(
        symbol="BTCUSDT",
        setup_id="bos_choch",
        direction="long",
        score=0.74,
        timeframe="15m",
        entry_low=100.0,
        entry_high=101.0,
        stop=98.0,
        take_profit_1=104.0,
        take_profit_2=106.0,
        reasons=(
            "bos_choch_long",
            "confluence_3_setups",
            "confluence_setups=bos_choch,fvg_setup,order_block",
        ),
    )

    text = format_signal_text(signal, pending_expiry_minutes=60)

    assert "<b>Confluence</b>" in text
    assert "3 setups" in text
    assert "bos_choch, fvg_setup, order_block" in text
