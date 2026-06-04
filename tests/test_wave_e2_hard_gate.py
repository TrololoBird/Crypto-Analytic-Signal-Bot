"""Wave E2: profile-aware hard confluence gate and unified MTF pass."""

from __future__ import annotations

from types import SimpleNamespace

import polars as pl

from bot.domain.labels import normalize_reject_reason, reject_reason_ru
from bot.domain.mtf import BREAKOUT_PROFILE
from bot.runtime.delivery_orchestrator import DeliveryOrchestrator


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
    closes = [close] * rows
    return pl.DataFrame(
        {
            "close": closes,
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
    microprice_bias: float | None = None,
    agg_trade_delta_30s: float | None = None,
    funding_rate: float = 0.0001,
    oi_change_pct: float = 1.0,
) -> SimpleNamespace:
    h1, h4 = _htf_frames(slope=0.5)
    return SimpleNamespace(
        work_15m=primary,
        work_1h=work_1h if work_1h is not None else h1,
        work_4h=work_4h if work_4h is not None else h4,
        microprice_bias=microprice_bias,
        agg_trade_delta_30s=agg_trade_delta_30s,
        funding_rate=funding_rate,
        oi_change_pct=oi_change_pct,
    )


def _signal(*, direction: str = "long", profile: str = "trend_follow") -> SimpleNamespace:
    return SimpleNamespace(direction=direction, confirmation_profile=profile)


def test_countertrend_long_passes_inverted_trend_leg() -> None:
    """Reversal long should not require close > ema20 > ema50."""
    prepared = _prepared(
        _primary(close=95.0, ema20=100.0, ema50=105.0, rsi=38.0),
        microprice_bias=0.08,
        agg_trade_delta_30s=0.02,
    )
    signal = _signal(direction="long", profile="countertrend_exhaustion")
    ok, confirmations, details = DeliveryOrchestrator._hard_confluence_gate(
        signal,  # type: ignore[arg-type]
        prepared,  # type: ignore[arg-type]
    )
    assert confirmations["trend"] is True
    assert confirmations["momentum"] is True
    assert confirmations["microstructure"] is True
    assert details["microstructure_source"] == "live_micro"
    assert ok is True


def test_trend_follow_long_fails_bearish_primary_stack() -> None:
    prepared = _prepared(
        _primary(close=95.0, ema20=100.0, ema50=105.0, rsi=25.0, volume=80.0),
        microprice_bias=None,
        agg_trade_delta_30s=None,
        funding_rate=0.01,
        oi_change_pct=20.0,
    )
    signal = _signal(direction="long", profile="trend_follow")
    ok, confirmations, _ = DeliveryOrchestrator._hard_confluence_gate(
        signal,  # type: ignore[arg-type]
        prepared,  # type: ignore[arg-type]
    )
    assert confirmations["trend"] is False
    assert confirmations["momentum"] is False
    assert ok is False


def test_breakout_accepts_lower_volume_multiplier() -> None:
    primary = _primary(close=110.0, ema20=105.0, ema50=100.0, rsi=55.0, volume=112.0)
    prepared = _prepared(primary, microprice_bias=0.06)
    signal = _signal(direction="long", profile=BREAKOUT_PROFILE)
    ok, confirmations, _ = DeliveryOrchestrator._hard_confluence_gate(
        signal,  # type: ignore[arg-type]
        prepared,  # type: ignore[arg-type]
    )
    assert confirmations["volume"] is True
    assert confirmations["trend"] is True
    assert ok is True


def test_microstructure_falls_back_to_funding_oi_proxy() -> None:
    prepared = _prepared(
        _primary(close=110.0, ema20=105.0, ema50=100.0, rsi=55.0),
        microprice_bias=None,
        agg_trade_delta_30s=None,
        funding_rate=0.0002,
        oi_change_pct=3.0,
    )
    signal = _signal(direction="long", profile="trend_follow")
    _ok, confirmations, details = DeliveryOrchestrator._hard_confluence_gate(
        signal,  # type: ignore[arg-type]
        prepared,  # type: ignore[arg-type]
    )
    assert confirmations["microstructure"] is True
    assert details["microstructure_source"] == "funding_oi_proxy"


def test_dual_htf_conflict_fails_htf_leg_in_hard_gate() -> None:
    h1, h4 = _htf_frames(slope=-0.8)
    prepared = _prepared(
        _primary(close=95.0, ema20=100.0, ema50=105.0, rsi=38.0),
        work_1h=h1,
        work_4h=h4,
        microprice_bias=0.08,
    )
    signal = _signal(direction="long", profile="divergence_reversal")
    ok, confirmations, details = DeliveryOrchestrator._hard_confluence_gate(
        signal,  # type: ignore[arg-type]
        prepared,  # type: ignore[arg-type]
        enforce_mtf_gate=True,
    )
    assert confirmations["htf"] is False
    assert "htf_reversal_conflict" in str(details.get("mtf_reason", ""))
    assert ok is False


def test_enforce_mtf_gate_false_skips_htf_leg() -> None:
    h1, h4 = _htf_frames(slope=-0.8)
    prepared = _prepared(
        _primary(close=95.0, ema20=100.0, ema50=105.0, rsi=38.0, volume=150.0),
        work_1h=h1,
        work_4h=h4,
        microprice_bias=0.08,
        agg_trade_delta_30s=0.01,
    )
    signal = _signal(direction="long", profile="divergence_reversal")
    ok, confirmations, details = DeliveryOrchestrator._hard_confluence_gate(
        signal,  # type: ignore[arg-type]
        prepared,  # type: ignore[arg-type]
        enforce_mtf_gate=False,
    )
    assert confirmations["htf"] is True
    assert details.get("mtf_enforced") is False
    assert ok is True


def test_hard_confluence_reject_reason_normalized() -> None:
    assert normalize_reject_reason("hard_confluence_gate_failed") == "hard_confluence_gate"
    assert reject_reason_ru("hard_confluence_gate_failed") == "слабый confluence"
