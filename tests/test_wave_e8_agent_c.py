"""Wave E8 agent C — delivery P0: reversal gate, portfolio caps, chase pct, companion."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import polars as pl
import pytest

from bot.delivery.deliver import SignalDelivery, format_analytics_companion
from bot.domain.config import BotSettings, DeliveryConfig, TrackingConfig
from bot.domain.limit_entry import DEFAULT_LATE_ENTRY_CHASE_PCT, resolve_late_entry_chase_pct
from bot.domain.schemas import Signal
from bot.runtime.delivery_orchestrator import DeliveryOrchestrator, MIN_CONFIRMATIONS


def _htf_frames(*, slope: float) -> tuple[pl.DataFrame, pl.DataFrame]:
    def frame(start: float, step: float) -> pl.DataFrame:
        prices = [start + step * idx for idx in range(60)]
        return pl.DataFrame(
            {
                "close": prices,
                "high": [p * 1.002 for p in prices],
                "low": [p * 0.998 for p in prices],
                "open": prices,
            }
        )

    return frame(120.0, slope), frame(130.0, slope * 0.5)


def _primary(
    *,
    close: float,
    ema20: float,
    ema50: float,
    rsi: float,
    volume: float = 80.0,
    base_volume: float = 100.0,
) -> pl.DataFrame:
    rows = 25
    volumes = [base_volume] * (rows - 1) + [volume]
    return pl.DataFrame(
        {
            "close": [close] * rows,
            "ema20": [ema20] * rows,
            "ema50": [ema50] * rows,
            "rsi14": [rsi] * rows,
            "volume": volumes,
        }
    )


def _prepared(
    primary: pl.DataFrame,
    *,
    work_1h: pl.DataFrame | None = None,
    work_4h: pl.DataFrame | None = None,
) -> SimpleNamespace:
    h1, h4 = _htf_frames(slope=0.5)
    return SimpleNamespace(
        work_15m=primary,
        work_1h=work_1h if work_1h is not None else h1,
        work_4h=work_4h if work_4h is not None else h4,
        microprice_bias=None,
        agg_trade_delta_30s=None,
        funding_rate=0.01,
        oi_change_pct=20.0,
    )


def _signal(**overrides: object) -> SimpleNamespace:
    base = {
        "direction": "long",
        "confirmation_profile": "countertrend_exhaustion",
        "btc_bias": "bear",
        "setup_id": "volume_climax_reversal",
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _orchestrator(**delivery_overrides: object) -> DeliveryOrchestrator:
    settings = BotSettings(
        tg_token="test",
        target_chat_id="1",
        delivery=DeliveryConfig(**delivery_overrides),
    )
    bot = SimpleNamespace(settings=settings)
    return DeliveryOrchestrator(bot)  # type: ignore[arg-type]


def _portfolio_signal(**overrides: object) -> Signal:
    base = {
        "symbol": "DOGEUSDT",
        "setup_id": "ema_bounce",
        "direction": "long",
        "score": 0.80,
        "timeframe": "15m",
        "entry_low": 99.0,
        "entry_high": 101.0,
        "stop": 95.0,
        "take_profit_1": 110.0,
        "take_profit_2": 115.0,
        "risk_reward": 2.0,
        "btc_bias": "neutral",
        "strategy_family": "continuation",
    }
    base.update(overrides)
    return Signal(**base)


# --- C1: reversal calibration ---


def test_reversal_volume_uses_1x_multiplier() -> None:
    """REVERSAL_PROFILES accept volume at 1.0x mean (not 1.2x)."""
    prepared = _prepared(
        _primary(close=95.0, ema20=100.0, ema50=105.0, rsi=38.0, volume=105.0),
        work_1h=_htf_frames(slope=0.5)[0],
        work_4h=_htf_frames(slope=0.5)[1],
    )
    signal = _signal(profile="countertrend_exhaustion")
    ok, confirmations, _ = DeliveryOrchestrator._hard_confluence_gate(
        signal,  # type: ignore[arg-type]
        prepared,  # type: ignore[arg-type]
        enforce_mtf_gate=False,
    )
    assert confirmations["volume"] is True
    assert ok is True


def test_bear_reversal_passes_with_two_confirmations() -> None:
    """Bear regime reversal profiles require reversal_min_confirmations (default 2)."""
    prepared = _prepared(
        _primary(close=95.0, ema20=100.0, ema50=105.0, rsi=70.0, volume=80.0),
    )
    signal = _signal(btc_bias="bear", confirmation_profile="countertrend_exhaustion")
    ok, confirmations, details = DeliveryOrchestrator._hard_confluence_gate(
        signal,  # type: ignore[arg-type]
        prepared,  # type: ignore[arg-type]
        enforce_mtf_gate=True,
        reversal_min_confirmations=2,
    )
    assert confirmations["trend"] is True
    assert confirmations["momentum"] is False
    assert sum(confirmations.values()) == 2
    assert details["required"] == 2
    assert details["bear_regime"] is True
    assert ok is True


def test_neutral_regime_reversal_still_requires_three() -> None:
    prepared = _prepared(
        _primary(close=95.0, ema20=100.0, ema50=105.0, rsi=70.0, volume=80.0),
    )
    signal = _signal(btc_bias="neutral", confirmation_profile="countertrend_exhaustion")
    ok, _, details = DeliveryOrchestrator._hard_confluence_gate(
        signal,  # type: ignore[arg-type]
        prepared,  # type: ignore[arg-type]
        enforce_mtf_gate=True,
        reversal_min_confirmations=2,
    )
    assert details["required"] == MIN_CONFIRMATIONS
    assert ok is False


def test_delivery_config_reversal_min_confirmations_default() -> None:
    delivery = DeliveryConfig()
    assert delivery.reversal_min_confirmations == 2


# --- C2: weighted confluence bridge flag ---


def test_use_weighted_confluence_sets_bridge_detail() -> None:
    prepared = _prepared(
        _primary(close=95.0, ema20=100.0, ema50=105.0, rsi=38.0, volume=150.0),
        work_1h=_htf_frames(slope=0.5)[0],
        work_4h=_htf_frames(slope=0.5)[1],
    )
    signal = _signal(microprice_bias=None)
    prepared.microprice_bias = 0.08  # type: ignore[attr-defined]
    prepared.agg_trade_delta_30s = 0.02  # type: ignore[attr-defined]
    prepared.funding_rate = 0.0001  # type: ignore[attr-defined]
    prepared.oi_change_pct = 1.0  # type: ignore[attr-defined]
    _, _, details = DeliveryOrchestrator._hard_confluence_gate(
        signal,  # type: ignore[arg-type]
        prepared,  # type: ignore[arg-type]
        enforce_mtf_gate=False,
        use_weighted_confluence=True,
    )
    assert details.get("weighted_confluence_bridge") is True


def test_use_weighted_confluence_default_false() -> None:
    assert DeliveryConfig().use_weighted_confluence is False


# --- C3: analytics companion ---


def test_format_analytics_companion_renders_html() -> None:
    signal = _portfolio_signal(setup_id="wick_trap_reversal", direction="long")
    text = format_analytics_companion(signal, btc_bias="bear", eth_bias="neutral")
    assert "WHY THIS SIGNAL" in text
    assert "DOGEUSDT" in text


@pytest.mark.asyncio
async def test_send_analytics_companion_calls_broadcaster() -> None:
    broadcaster = MagicMock()
    broadcaster.send_html = AsyncMock(return_value=SimpleNamespace(status="sent"))
    delivery = SignalDelivery(broadcaster, pending_expiry_minutes=180)
    signal = _portfolio_signal()
    await delivery.send_analytics_companion(signal, btc_bias="bear", eth_bias="neutral")
    broadcaster.send_html.assert_awaited_once()


# --- C4: portfolio caps from DeliveryConfig ---


def test_portfolio_cap_same_direction_regime_from_config() -> None:
    orch = _orchestrator(portfolio_max_same_direction_regime=1)
    state = orch._new_portfolio_cap_state()
    first = _portfolio_signal(symbol="DOGEUSDT", direction="long", btc_bias="neutral")
    second = _portfolio_signal(symbol="ADAUSDT", direction="long", btc_bias="neutral")
    ok1, _ = orch._passes_portfolio_cap(first, state)
    ok2, reason = orch._passes_portfolio_cap(second, state)
    assert ok1 is True
    assert ok2 is False
    assert reason == "portfolio_direction_regime_cap"


def test_portfolio_cap_bear_longs_from_config() -> None:
    orch = _orchestrator(portfolio_max_bear_longs=1)
    state = orch._new_portfolio_cap_state()
    first = _portfolio_signal(symbol="DOGEUSDT", direction="long", btc_bias="bear")
    second = _portfolio_signal(symbol="ADAUSDT", direction="long", btc_bias="downtrend")
    ok1, _ = orch._passes_portfolio_cap(first, state)
    ok2, reason = orch._passes_portfolio_cap(second, state)
    assert ok1 is True
    assert ok2 is False
    assert reason == "portfolio_long_btc_bear_cap"


def test_portfolio_cap_family_direction_from_config() -> None:
    orch = _orchestrator(portfolio_max_family_direction=1)
    state = orch._new_portfolio_cap_state()
    first = _portfolio_signal(
        symbol="DOGEUSDT",
        strategy_family="reversal",
        direction="short",
        btc_bias="bear",
        entry_low=99.0,
        entry_high=101.0,
        stop=105.0,
        take_profit_1=88.0,
        take_profit_2=85.0,
        risk_reward=2.4,
    )
    second = _portfolio_signal(
        symbol="ADAUSDT",
        strategy_family="reversal",
        direction="short",
        btc_bias="bear",
        entry_low=99.0,
        entry_high=101.0,
        stop=105.0,
        take_profit_1=88.0,
        take_profit_2=85.0,
        risk_reward=2.4,
    )
    ok1, _ = orch._passes_portfolio_cap(first, state)
    ok2, reason = orch._passes_portfolio_cap(second, state)
    assert ok1 is True
    assert ok2 is False
    assert reason == "portfolio_family_direction_cap"


# --- C5: late_entry_chase_pct single source ---


def test_resolve_late_entry_chase_pct_from_tracking() -> None:
    settings = BotSettings(
        tg_token="test",
        target_chat_id="1",
        tracking=TrackingConfig(late_entry_chase_pct=0.012),
    )
    assert resolve_late_entry_chase_pct(settings) == 0.012


def test_resolve_late_entry_chase_pct_domain_default() -> None:
    settings = BotSettings(tg_token="test", target_chat_id="1")
    assert resolve_late_entry_chase_pct(settings) == DEFAULT_LATE_ENTRY_CHASE_PCT
    assert DEFAULT_LATE_ENTRY_CHASE_PCT == 0.008
    assert settings.tracking.late_entry_chase_pct == DEFAULT_LATE_ENTRY_CHASE_PCT


def test_orchestrator_limit_gate_uses_tracking_chase_pct() -> None:
    settings = BotSettings(
        tg_token="test",
        target_chat_id="1",
        tracking=TrackingConfig(late_entry_chase_pct=0.02),
    )
    bot = SimpleNamespace(settings=settings)
    orch = DeliveryOrchestrator(bot)  # type: ignore[arg-type]
    signal = SimpleNamespace(
        direction="long",
        mark_price=102.0,
        entry_low=100.0,
        entry_high=101.0,
        stop=90.0,
    )
    ready, reason, details = orch._limit_entry_gate(signal, None)  # type: ignore[arg-type]
    assert ready is True
    assert reason is None
    assert details["mark_price"] == 102.0
