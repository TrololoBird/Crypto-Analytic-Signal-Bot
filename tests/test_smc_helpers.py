from __future__ import annotations

import polars as pl

from bot.setups.smc import (
    bos_choch,
    fvg,
    latest_fvg_zone,
    liquidity_pools,
    order_blocks,
    swing_highs_lows,
)


def _eurusd_ohlcv() -> pl.DataFrame:
    return pl.DataFrame(
        {
            "open": [
                1.0,
                1.1,
                1.2,
                1.15,
                1.3,
                1.25,
                1.35,
                1.4,
                1.32,
                1.45,
                1.5,
                1.55,
            ],
            "high": [
                1.05,
                1.15,
                1.25,
                1.2,
                1.35,
                1.3,
                1.4,
                1.45,
                1.37,
                1.5,
                1.55,
                1.6,
            ],
            "low": [
                0.95,
                1.05,
                1.15,
                1.1,
                1.2,
                1.2,
                1.25,
                1.3,
                1.28,
                1.35,
                1.4,
                1.45,
            ],
            "close": [
                1.02,
                1.12,
                1.18,
                1.18,
                1.28,
                1.27,
                1.38,
                1.33,
                1.36,
                1.48,
                1.52,
                1.58,
            ],
            "volume": [100.0] * 12,
        }
    )


def test_fvg_schema_and_length() -> None:
    actual = fvg(_eurusd_ohlcv())
    assert actual.columns == ["FVG", "Top", "Bottom", "MitigatedIndex"]
    assert actual.height == _eurusd_ohlcv().height


def test_fvg_join_consecutive_is_not_more_noisy_than_default() -> None:
    frame = _eurusd_ohlcv()
    raw = fvg(frame)
    joined = fvg(frame, join_consecutive=True)
    raw_count = raw["FVG"].drop_nulls().len()
    joined_count = joined["FVG"].drop_nulls().len()
    assert joined.columns == raw.columns
    assert joined.height == raw.height
    assert joined_count <= raw_count


def test_swing_highs_lows_schema_and_length() -> None:
    actual = swing_highs_lows(_eurusd_ohlcv(), swing_length=5, mode="offline_parity")
    assert actual.columns == ["HighLow", "Level"]
    assert actual.height == _eurusd_ohlcv().height


def test_bos_choch_schema_and_length() -> None:
    frame = _eurusd_ohlcv()
    swings = swing_highs_lows(frame, swing_length=5, mode="offline_parity")
    actual = bos_choch(frame, swings)
    assert actual.columns == ["BOS", "CHOCH", "Level", "BrokenIndex"]
    assert actual.height == frame.height


def test_order_blocks_schema_and_length() -> None:
    frame = _eurusd_ohlcv()
    swings = swing_highs_lows(frame, swing_length=5, mode="offline_parity")
    actual = order_blocks(frame, swings)
    assert actual.columns == [
        "OB",
        "Top",
        "Bottom",
        "OBVolume",
        "MitigatedIndex",
        "Percentage",
    ]
    assert actual.height == frame.height


def test_liquidity_schema_and_length() -> None:
    frame = _eurusd_ohlcv()
    swings = swing_highs_lows(frame, swing_length=5, mode="offline_parity")
    actual = liquidity_pools(frame, swings)
    assert actual.columns == ["Liquidity", "Level", "End", "Swept"]
    assert actual.height == frame.height


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
