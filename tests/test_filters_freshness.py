from __future__ import annotations

from datetime import UTC, datetime, timedelta

import polars as pl

from bot.filters import _frame_is_fresh


def _frame_with_close(close_time: datetime) -> pl.DataFrame:
    return pl.DataFrame({"close_time": [close_time], "close": [1.0]})


def test_fresh_when_forming_candle_close_time_in_future() -> None:
    now = datetime.now(UTC)
    future_close = now + timedelta(minutes=10)
    frame = _frame_with_close(future_close)
    assert _frame_is_fresh(frame, timedelta(minutes=16), timeframe="15m")


def test_fresh_at_new_15m_bar_open_with_previous_close() -> None:
    now = datetime.now(UTC)
    interval = 900
    period_start = datetime.fromtimestamp(int(now.timestamp()) - int(now.timestamp()) % interval, tz=UTC)
    previous_close = period_start - timedelta(minutes=15)
    frame = _frame_with_close(previous_close)
    assert _frame_is_fresh(frame, timedelta(minutes=16), timeframe="15m")


def test_stale_when_missing_entire_15m_candle() -> None:
    now = datetime.now(UTC)
    stale_close = now - timedelta(minutes=35)
    frame = _frame_with_close(stale_close)
    assert not _frame_is_fresh(frame, timedelta(minutes=16), timeframe="15m")
