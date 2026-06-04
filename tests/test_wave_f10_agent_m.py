"""Wave F10 Agent M: SMC helpers, trade plan, bos_choch spec tier."""

from __future__ import annotations

from unittest.mock import patch

import polars as pl
import pytest

from bot.setups.smc import (
    SMCZone,
    fvg_ce_entry,
    is_clean_fvg,
    sweep_tolerance,
    swing_series,
)
from bot.setups.utils import build_smc_trade_plan
from bot.strategies.bos_choch import _spec_detect_kwargs, detect_bos_choch
from bot.strategies.fvg import detect_fvg
from bot.strategies.liquidity_sweep import detect_liquidity_sweep


def _ohlc_frame(rows: list[tuple[float, float, float, float]]) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "open": [row[0] for row in rows],
            "high": [row[1] for row in rows],
            "low": [row[2] for row in rows],
            "close": [row[3] for row in rows],
            "volume": [1000.0] * len(rows),
        }
    )


def _bullish_fvg_rows() -> list[tuple[float, float, float, float]]:
    pad = (100.0, 101.0, 99.0, 100.5)
    return [
        pad,
        pad,
        (100.0, 101.0, 99.0, 100.5),
        (100.5, 103.0, 100.0, 102.5),
        (102.5, 103.0, 104.0, 103.5),
    ]


def test_is_clean_fvg_accepts_balanced_impulse_gap() -> None:
    frame = _ohlc_frame(_bullish_fvg_rows())
    assert is_clean_fvg(frame, created_index=3, direction="long") is True


def test_is_clean_fvg_rejects_bridging_middle_candle() -> None:
    rows = _bullish_fvg_rows()
    rows[3] = (100.5, 104.5, 100.0, 101.8)
    frame = _ohlc_frame(rows)
    assert is_clean_fvg(frame, created_index=3, direction="long") is False


def test_fvg_ce_entry_clamps_to_gap_edge_for_live_price() -> None:
    assert fvg_ce_entry(bottom=100.0, top=102.0, direction="long", price=101.0) == pytest.approx(101.0)
    assert fvg_ce_entry(bottom=100.0, top=102.0, direction="short", price=101.0) == pytest.approx(101.0)
    assert fvg_ce_entry(bottom=100.0, top=102.0, direction="long") == pytest.approx(101.0)


def test_sweep_tolerance_uses_max_of_atr_and_pct() -> None:
    by_atr = sweep_tolerance(level=100.0, atr=2.0, sweep_atr_mult=0.2)
    by_pct = sweep_tolerance(level=100.0, atr=2.0, sweep_atr_mult=0.2, tolerance_pct=0.01)
    assert by_atr == pytest.approx(0.4)
    assert by_pct == pytest.approx(1.0)


def test_swing_series_exports_aligned_masks() -> None:
    frame = _ohlc_frame([(100.0, 101.0, 99.0, 100.5)] * 12)
    pivots = swing_series(frame, swing_length=2, include_unconfirmed_tail=True)
    assert pivots.high_mask.len() == frame.height
    assert pivots.low_mask.len() == frame.height
    assert pivots.swings.height == frame.height
    assert pivots.high_mask.dtype == pl.Boolean
    assert pivots.swings.columns == ["HighLow", "Level"]


def test_build_smc_trade_plan_long_structural_fallback() -> None:
    frame = pl.DataFrame(
        {
            "high": [100.0, 101.0, 102.0, 103.0, 104.0],
            "low": [99.0, 100.0, 101.0, 102.0, 103.0],
        }
    )
    plan = build_smc_trade_plan(
        direction="long",
        price_anchor=100.5,
        stop_basis=99.5,
        atr=1.0,
        work_1h=frame,
        min_rr=1.5,
        sl_buffer_atr=0.5,
    )
    assert plan is not None
    assert plan.stop < plan.entry
    assert plan.tp1 > plan.entry
    assert plan.tp2 > plan.tp1
    assert plan.risk == pytest.approx(plan.entry - plan.stop)


def test_detect_fvg_spec_skips_non_clean_gap() -> None:
    rows = _bullish_fvg_rows()
    rows[3] = (100.5, 104.5, 99.5, 101.8)
    frame = _ohlc_frame(rows)
    assert detect_fvg(frame, max_age=5) is None


def test_detect_liquidity_sweep_spec_uses_sweep_tolerance() -> None:
    frame = pl.DataFrame(
        {
            "open": [100.0] * 30,
            "high": [100.5] * 29 + [101.2],
            "low": [99.5] * 30,
            "close": [100.2] * 29 + [99.8],
            "volume": [1000.0] * 30,
            "spec_atr14": [1.0] * 30,
            "spec_prev_high20": [100.0] * 30,
            "spec_prev_low20": [99.0] * 30,
            "volume_ratio20": [1.0] * 30,
            "rsi14": [50.0] * 30,
            "_spec_idx": list(range(30)),
        }
    )
    tol = sweep_tolerance(level=100.0, atr=1.0, sweep_atr_mult=0.2)
    assert tol == pytest.approx(0.2)
    hit = detect_liquidity_sweep(frame, sweep_atr_mult=0.2)
    assert hit is not None
    assert hit.direction == "short"


@patch("bot.strategies.bos_choch.latest_structure_break")
def test_detect_bos_choch_spec_uses_latest_structure_break(mock_break) -> None:
    zone = SMCZone(
        kind="choch",
        direction="long",
        top=105.0,
        bottom=105.0,
        created_index=20,
        state="mitigated",
        midpoint=105.0,
        width=0.0,
        level=105.0,
        broken_index=22,
    )
    mock_break.return_value = zone
    frame = pl.DataFrame(
        {
            "open": [100.0] * 30,
            "high": [101.0] * 30,
            "low": [99.0] * 30,
            "close": [106.0] * 30,
            "volume": [1000.0] * 30,
            "spec_atr14": [1.0] * 30,
            "rsi14": [50.0] * 30,
            "volume_ratio20": [1.0] * 30,
            "_spec_idx": list(range(30)),
        }
    )
    hit = detect_bos_choch(frame, max_age=10, swing_length=5)
    assert hit is not None
    assert hit.direction == "long"
    assert hit.source_index == 22
    mock_break.assert_called_once()
    assert mock_break.call_args.kwargs["prefer_kind"] == "choch"
    assert "choch_break_above" in hit.reasons[0]


def test_spec_detect_kwargs_wires_swing_length_not_donchian() -> None:
    kwargs = _spec_detect_kwargs({"swing_lookback": 7, "max_break_age_bars": 12})
    assert kwargs == {"max_age": 12, "swing_length": 7}
