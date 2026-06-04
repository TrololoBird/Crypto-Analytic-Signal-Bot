"""Delivery orchestrator MTF + hard confluence integration tests."""

from __future__ import annotations

from types import SimpleNamespace

import polars as pl

from bot.runtime.delivery_orchestrator import DeliveryOrchestrator


def _prepared_bearish() -> SimpleNamespace:
    prices = [120.0 - 0.8 * idx for idx in range(60)]
    frame = pl.DataFrame(
        {
            "close": prices,
            "high": [p * 1.002 for p in prices],
            "low": [p * 0.998 for p in prices],
            "open": prices,
        }
    )
    primary = pl.DataFrame(
        {
            "close": [95.0] * 20,
            "ema20": [100.0] * 20,
            "ema50": [105.0] * 20,
            "rsi14": [38.0] * 20,
            "volume": [100.0] * 19 + [150.0],
        }
    )
    return SimpleNamespace(work_1h=frame, work_4h=frame, work_15m=primary)


def test_hard_gate_blocks_reversal_with_dual_htf_conflict() -> None:
    """Unified MTF: htf leg inside hard confluence gate (no separate _mtf_delivery_gate)."""
    signal = SimpleNamespace(
        direction="long",
        confirmation_profile="divergence_reversal",
    )
    ok, confirmations, details = DeliveryOrchestrator._hard_confluence_gate(
        signal,  # type: ignore[arg-type]
        _prepared_bearish(),  # type: ignore[arg-type]
        enforce_mtf_gate=True,
    )
    assert confirmations["htf"] is False
    assert ok is False
    assert "htf_reversal_conflict" in str(details.get("mtf_reason", ""))


def test_hard_gate_skips_htf_when_mtf_enforcement_disabled() -> None:
    signal = SimpleNamespace(direction="long", confirmation_profile="trend_follow")
    ok, confirmations, details = DeliveryOrchestrator._hard_confluence_gate(
        signal,  # type: ignore[arg-type]
        _prepared_bearish(),  # type: ignore[arg-type]
        enforce_mtf_gate=False,
    )
    assert confirmations["htf"] is True
    assert details.get("mtf_enforced") is False
