from __future__ import annotations

from pathlib import Path

import numpy as np
import polars as pl
import pytest

from bot.setups.smc import (
    bos_choch,
    fvg,
    latest_fvg_zone,
    liquidity_pools,
    order_blocks,
    swing_highs_lows,
)

DATA_DIR = Path("audit/smc_lib/tests/test_data/EURUSD")
pytestmark = pytest.mark.skipif(
    not DATA_DIR.exists(),
    reason="optional integration fixture tree audit/smc_lib is not available",
)


def _eurusd_ohlcv() -> pl.DataFrame:
    frame = pl.read_csv(DATA_DIR / "EURUSD_15M.csv")
    frame = frame.rename({column: column.lower() for column in frame.columns})
    return frame.select(["open", "high", "low", "close", "volume"])


def _reference_frame(filename: str) -> pl.DataFrame:
    frame = pl.read_csv(DATA_DIR / filename, null_values=[""])
    return frame.with_columns([pl.all().cast(pl.Float64, strict=False)])


def _assert_frame_close(actual: pl.DataFrame, expected: pl.DataFrame) -> None:
    assert actual.columns == expected.columns
    assert actual.height == expected.height
    for column in actual.columns:
        actual_values = (
            actual[column]
            .cast(pl.Float64, strict=False)
            .fill_null(float("nan"))
            .to_numpy()
        )
        expected_values = (
            expected[column]
            .cast(pl.Float64, strict=False)
            .fill_null(float("nan"))
            .to_numpy()
        )
        np.testing.assert_allclose(
            actual_values,
            expected_values,
            equal_nan=True,
            err_msg=f"column mismatch: {column}",
        )


def test_fvg_matches_reference_fixture() -> None:
    actual = fvg(_eurusd_ohlcv())
    expected = _reference_frame("fvg_result_data.csv")
    _assert_frame_close(actual, expected)


def test_fvg_join_consecutive_matches_reference_fixture() -> None:
    actual = fvg(_eurusd_ohlcv(), join_consecutive=True)
    expected = _reference_frame("fvg_consecutive_result_data.csv")
    _assert_frame_close(actual, expected)


def test_swing_highs_lows_matches_reference_fixture() -> None:
    actual = swing_highs_lows(_eurusd_ohlcv(), swing_length=5, mode="offline_parity")
    expected = _reference_frame("swing_highs_lows_result_data.csv")
    _assert_frame_close(actual, expected)


def test_bos_choch_matches_reference_fixture() -> None:
    frame = _eurusd_ohlcv()
    swings = swing_highs_lows(frame, swing_length=5, mode="offline_parity")
    actual = bos_choch(frame, swings)
    expected = _reference_frame("bos_choch_result_data.csv")
    _assert_frame_close(actual, expected)


def test_order_blocks_matches_reference_fixture() -> None:
    frame = _eurusd_ohlcv()
    swings = swing_highs_lows(frame, swing_length=5, mode="offline_parity")
    actual = order_blocks(frame, swings)
    expected = _reference_frame("ob_result_data.csv")
    _assert_frame_close(actual, expected)


def test_liquidity_matches_reference_fixture() -> None:
    frame = _eurusd_ohlcv()
    swings = swing_highs_lows(frame, swing_length=5, mode="offline_parity")
    actual = liquidity_pools(frame, swings)
    expected = _reference_frame("liquidity_result_data.csv")
    _assert_frame_close(actual, expected)


def test_latest_fvg_zone_reports_invalidation_state() -> None:
    frame = pl.DataFrame(
        {
            "open": [9.8, 9.7, 10.7, 10.6, 10.1],
            "high": [10.0, 10.9, 11.2, 10.8, 10.3],
            "low": [9.4, 9.6, 10.5, 10.4, 9.8],
            "close": [9.6, 10.8, 11.0, 10.5, 10.0],
            "volume": [100.0, 110.0, 120.0, 130.0, 140.0],
        }
    )

    zone = latest_fvg_zone(
        frame,
        join_consecutive=False,
        allowed_states=("invalidated",),
    )

    assert zone is not None
    assert zone.direction == "long"
    assert zone.state == "invalidated"
    assert zone.invalidation_index == 4
