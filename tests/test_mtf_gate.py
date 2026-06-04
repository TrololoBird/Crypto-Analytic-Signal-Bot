"""Tests for shared MTF delivery gate."""

from __future__ import annotations

from types import SimpleNamespace

import polars as pl

from bot.domain.mtf import evaluate_mtf_gate, normalize_mtf_reject_reason


def _prepared(*, slope: float) -> SimpleNamespace:
    """Build 1h/4h frames with monotonic close series (slope < 0 = bearish HTF)."""

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

    return SimpleNamespace(
        work_1h=frame(120.0, slope),
        work_4h=frame(130.0, slope * 0.5),
    )


def test_trend_follow_blocks_long_against_bearish_htf() -> None:
    ok, reason, _ = evaluate_mtf_gate(
        _prepared(slope=-0.8),
        "long",
        confirmation_profile="trend_follow",
    )
    assert ok is False
    assert "htf_conflict" in reason or "bearish" in reason


def test_reversal_blocks_when_both_htf_oppose() -> None:
    ok, reason, details = evaluate_mtf_gate(
        _prepared(slope=-0.8),
        "long",
        confirmation_profile="divergence_reversal",
    )
    assert ok is False
    assert reason.startswith("htf_reversal_conflict")
    assert set(details.get("conflicts") or []) == {"1h", "4h"}


def test_reversal_allows_long_with_single_bearish_htf() -> None:
    prep = _prepared(slope=-0.8)
    prep.work_4h = pl.DataFrame(
        {
            "close": [130.0 + 0.4 * idx for idx in range(60)],
            "high": [131.0 + 0.4 * idx for idx in range(60)],
            "low": [129.0 + 0.4 * idx for idx in range(60)],
            "open": [130.0 + 0.4 * idx for idx in range(60)],
        }
    )
    ok, _, details = evaluate_mtf_gate(
        prep,
        "long",
        confirmation_profile="divergence_reversal",
    )
    assert ok is True
    assert details.get("conflicts") == ["1h"]


def test_normalize_mtf_reject_reason() -> None:
    assert normalize_mtf_reject_reason("htf_reversal_conflict:1h,4h") == "htf_reversal_conflict"
    assert normalize_mtf_reject_reason("htf_conflict:bearish") == "htf_conflict"


def test_breakout_profile_allows_relaxed_htf_on_trend_follow_logic() -> None:
    """breakout_acceptance uses strict EMA trend on HTF via evaluate_mtf_gate."""
    ok, reason, _ = evaluate_mtf_gate(
        _prepared(slope=-0.8),
        "long",
        confirmation_profile="breakout_acceptance",
    )
    assert ok is False
    assert "htf_conflict" in reason or "bearish" in reason
