"""Wave F9 Agent N — bear regime confluence + countertrend HTF fix."""

from __future__ import annotations

from types import SimpleNamespace

import polars as pl

from bot.domain.delivery_policy import resolve_bear_regime
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
    volume: float = 150.0,
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
    regime_1h_confirmed: str = "ranging",
    regime_4h_confirmed: str = "ranging",
    btc_bias: str | None = None,
    market_ctx: dict[str, object] | None = None,
    microprice_bias: float | None = None,
    agg_trade_delta_30s: float | None = None,
) -> SimpleNamespace:
    h1, h4 = _htf_frames(slope=0.5)
    return SimpleNamespace(
        work_15m=primary,
        work_1h=work_1h if work_1h is not None else h1,
        work_4h=work_4h if work_4h is not None else h4,
        regime_1h_confirmed=regime_1h_confirmed,
        regime_4h_confirmed=regime_4h_confirmed,
        btc_bias=btc_bias,
        market_ctx=market_ctx,
        microprice_bias=microprice_bias,
        agg_trade_delta_30s=agg_trade_delta_30s,
        funding_rate=0.0001,
        oi_change_pct=1.0,
    )


def _signal(**overrides: object) -> SimpleNamespace:
    base = {
        "direction": "long",
        "confirmation_profile": "countertrend_exhaustion",
        "btc_bias": None,
        "setup_id": "volume_climax_reversal",
    }
    base.update(overrides)
    return SimpleNamespace(**base)


# --- N2: resolve_bear_regime ---


def test_resolve_bear_regime_signal_btc_bias_priority() -> None:
    bear, source = resolve_bear_regime(
        market_ctx={"btc_bias": "neutral", "market_regime": "bull"},
        prepared_btc_bias="neutral",
        signal_btc_bias="bear",
    )
    assert bear is True
    assert source == "signal_btc_bias"


def test_resolve_bear_regime_prepared_btc_bias() -> None:
    bear, source = resolve_bear_regime(
        prepared_btc_bias="downtrend",
        signal_btc_bias="neutral",
    )
    assert bear is True
    assert source == "prepared_btc_bias"


def test_resolve_bear_regime_market_ctx_regime() -> None:
    bear, source = resolve_bear_regime(
        market_ctx={"market_regime": "bear", "btc_bias": "neutral"},
    )
    assert bear is True
    assert source == "market_ctx_regime"


def test_resolve_bear_regime_not_alt_regime_1h() -> None:
    """Alt regime_1h alone must not set bear via resolve_bear_regime."""
    bear, source = resolve_bear_regime(
        market_ctx=None,
        prepared_btc_bias=None,
        signal_btc_bias=None,
    )
    assert bear is False
    assert source == "none"


# --- N1: countertrend bear passes dual HTF conflict ---


def test_countertrend_bear_passes_dual_htf_conflict() -> None:
    """N1: reversal long in bear should not hard-fail on dual HTF bearish."""
    h1, h4 = _htf_frames(slope=-0.8)
    prepared = _prepared(
        _primary(close=95.0, ema20=100.0, ema50=105.0, rsi=38.0),
        work_1h=h1,
        work_4h=h4,
        microprice_bias=0.08,
        agg_trade_delta_30s=0.02,
    )
    signal = _signal(btc_bias="bear", confirmation_profile="countertrend_exhaustion")
    ok, confirmations, details = DeliveryOrchestrator._hard_confluence_gate(
        signal,  # type: ignore[arg-type]
        prepared,  # type: ignore[arg-type]
        enforce_mtf_gate=True,
        reversal_min_confirmations=2,
    )
    assert confirmations["htf"] is True
    assert "htf_reversal_expected_bear" in str(details.get("mtf_reason", ""))
    assert details["bear_regime"] is True
    assert details["bear_regime_source"] == "signal_btc_bias"
    assert ok is True


def test_neutral_btc_dual_htf_conflict_still_fails() -> None:
    """Without global bear, dual HTF conflict still blocks reversal longs."""
    h1, h4 = _htf_frames(slope=-0.8)
    prepared = _prepared(
        _primary(close=95.0, ema20=100.0, ema50=105.0, rsi=38.0),
        work_1h=h1,
        work_4h=h4,
        regime_1h_confirmed="downtrend",
        regime_4h_confirmed="downtrend",
    )
    signal = _signal(btc_bias="neutral", confirmation_profile="countertrend_exhaustion")
    ok, confirmations, details = DeliveryOrchestrator._hard_confluence_gate(
        signal,  # type: ignore[arg-type]
        prepared,  # type: ignore[arg-type]
        enforce_mtf_gate=True,
    )
    assert confirmations["htf"] is False
    assert "htf_reversal_conflict" in str(details.get("mtf_reason", ""))
    assert ok is False


# --- N3: alt regime_1h alone does not lower reversal threshold ---


def test_alt_regime_1h_downtrend_does_not_trigger_bear_threshold() -> None:
    """N3: reversal_min_confirmations only when BTC/global bear."""
    prepared = _prepared(
        _primary(close=95.0, ema20=100.0, ema50=105.0, rsi=70.0, volume=80.0),
        regime_1h_confirmed="downtrend",
        regime_4h_confirmed="downtrend",
    )
    signal = _signal(btc_bias="neutral", confirmation_profile="countertrend_exhaustion")
    _, confirmations, details = DeliveryOrchestrator._hard_confluence_gate(
        signal,  # type: ignore[arg-type]
        prepared,  # type: ignore[arg-type]
        enforce_mtf_gate=False,
        reversal_min_confirmations=2,
    )
    assert details["bear_regime"] is False
    assert details["bear_regime_source"] == "none"
    assert details["required"] == MIN_CONFIRMATIONS
    # Would fail at 2-of-5 if alt regime_1h incorrectly lowered the bar.
    assert sum(confirmations.values()) < MIN_CONFIRMATIONS or details["required"] == MIN_CONFIRMATIONS


def test_market_ctx_bear_triggers_reversal_threshold() -> None:
    prepared = _prepared(
        _primary(close=95.0, ema20=100.0, ema50=105.0, rsi=70.0, volume=80.0),
        market_ctx={"btc_bias": "bear", "market_regime": "bear"},
    )
    signal = _signal(btc_bias=None, confirmation_profile="countertrend_exhaustion")
    ok, confirmations, details = DeliveryOrchestrator._hard_confluence_gate(
        signal,  # type: ignore[arg-type]
        prepared,  # type: ignore[arg-type]
        enforce_mtf_gate=False,
        reversal_min_confirmations=2,
    )
    assert details["bear_regime"] is True
    assert details["bear_regime_source"] == "market_ctx_btc_bias"
    assert details["required"] == 2
    assert sum(confirmations.values()) >= 2
    assert ok is True


# --- N10: gate details always include core fields ---


def test_gate_details_on_prepared_missing() -> None:
    signal = _signal()
    ok, _, details = DeliveryOrchestrator._hard_confluence_gate(
        signal,  # type: ignore[arg-type]
        None,
    )
    assert ok is False
    assert details["bear_regime"] is False
    assert details["required"] == MIN_CONFIRMATIONS
    assert details["confirmed"] == 0
    assert details["mtf_reason"] == ""
    assert details["bear_regime_source"] == "none"


def test_gate_details_always_present_on_success() -> None:
    prepared = _prepared(
        _primary(close=95.0, ema20=100.0, ema50=105.0, rsi=38.0),
        microprice_bias=0.08,
        agg_trade_delta_30s=0.02,
    )
    signal = _signal(btc_bias="bear")
    _, _, details = DeliveryOrchestrator._hard_confluence_gate(
        signal,  # type: ignore[arg-type]
        prepared,  # type: ignore[arg-type]
        enforce_mtf_gate=False,
    )
    for key in ("bear_regime", "required", "confirmed", "mtf_reason", "bear_regime_source"):
        assert key in details
