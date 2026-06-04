"""Wave F9 Agent M: unified FVG 3-candle index, OB 15m pattern TF, SMC spec tier."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import patch

import polars as pl
import pytest

from bot.domain.config import BotSettings
from bot.domain.schemas import PreparedSymbol, UniverseSymbol
from bot.setups.smc import SMCZone, fvg_candidates
from bot.strategies.fvg import detect_fvg
from bot.strategies.order_block import (
    OrderBlockSetup,
    _detect_order_block_extended,
    _spec_detect_kwargs,
    detect_order_block,
    detect_order_block_setup,
)


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


def _universe() -> UniverseSymbol:
    return UniverseSymbol(
        symbol="BTCUSDT",
        base_asset="BTC",
        quote_asset="USDT",
        contract_type="PERPETUAL",
        status="TRADING",
        onboard_date_ms=0,
        quote_volume=1e9,
        price_change_pct=1.0,
        last_price=102.0,
    )


def _prepared(**overrides: object) -> PreparedSymbol:
    frame = pl.DataFrame(
        {
            "open": [100.0] * 40,
            "high": [101.0] * 40,
            "low": [99.0] * 40,
            "close": [100.0] * 40,
            "volume": [1000.0] * 40,
            "atr14": [1.0] * 40,
            "rsi14": [50.0] * 40,
            "volume_ratio20": [1.2] * 40,
            "close_position": [0.5] * 40,
            "time": [datetime(2026, 6, 3, 10, 0, tzinfo=UTC)] * 40,
        }
    )
    base = PreparedSymbol(
        universe=_universe(),
        work_1h=frame,
        work_5m=frame,
        work_15m=frame,
        work_4h=frame,
        work_primary=frame,
        bid_price=100.0,
        ask_price=100.1,
        spread_bps=10.0,
        primary_timeframe="15m",
        mark_price=102.0,
    )
    for key, value in overrides.items():
        setattr(base, key, value)
    return base


def test_detect_fvg_source_index_matches_smc_middle_candle() -> None:
    pad = (100.0, 101.0, 99.0, 100.5)
    frame = _ohlc_frame(
        [
            pad,
            pad,
            (100.0, 101.0, 99.0, 100.5),
            (100.5, 103.0, 100.0, 102.5),
            (102.5, 103.0, 104.0, 103.5),
        ]
    )
    smc_idx = next(idx for idx, direction, _, _ in fvg_candidates(frame, max_age=5) if direction == "long")
    hit = detect_fvg(frame, max_age=5)
    assert hit is not None
    assert hit.source_index == smc_idx
    assert hit.source_index == 3


def test_fvg_candidates_respects_max_age() -> None:
    pad = (100.0, 101.0, 99.0, 100.5)
    rows = [pad] * 30
    rows.extend(
        [
            (100.0, 101.0, 99.0, 100.5),
            (100.5, 103.0, 100.0, 102.5),
            (102.5, 103.0, 104.0, 103.5),
        ]
    )
    frame = _ohlc_frame(rows)
    recent = fvg_candidates(frame, max_age=2)
    all_candidates = fvg_candidates(frame, max_age=50)
    assert len(recent) <= len(all_candidates)
    assert all_candidates
    assert recent[0][0] == all_candidates[0][0]


def test_spec_detect_kwargs_wires_ob_max_age() -> None:
    kwargs = _spec_detect_kwargs({"ob_max_age": 48.0, "touch_buffer_atr": 0.3})
    assert kwargs == {"ob_max_age": 48, "touch_buffer_atr": 0.3}


def test_detect_order_block_respects_ob_max_age_param() -> None:
    zone = SMCZone(
        kind="order_block",
        direction="long",
        top=101.0,
        bottom=99.0,
        created_index=5,
        state="fresh",
        midpoint=100.0,
        width=2.0,
    )
    frame = _prepared().work_15m
    with patch("bot.strategies.order_block.latest_order_block", return_value=zone):
        too_old = detect_order_block(frame, ob_max_age=10)
        fresh = detect_order_block(frame, ob_max_age=50)
    assert too_old is None
    assert fresh is not None
    assert fresh.direction == "long"


@patch("bot.strategies.order_block.latest_order_block", return_value=None)
def test_detect_order_block_spec_uses_latest_order_block(mock_lob) -> None:
    frame = _prepared().work_15m
    assert detect_order_block(frame) is None
    mock_lob.assert_called_once()
    call_kwargs = mock_lob.call_args.kwargs
    assert call_kwargs["current_price"] == pytest.approx(100.0)
    assert call_kwargs["touch_buffer"] == pytest.approx(0.25)


@patch("bot.strategies.order_block.latest_order_block", return_value=None)
def test_extended_order_block_scans_work_15m(mock_lob) -> None:
    frame_15m = pl.DataFrame(
        {
            "open": [100.0] * 40,
            "high": [101.0] * 40,
            "low": [99.0] * 40,
            "close": [100.0] * 40,
            "volume": [1000.0] * 40,
            "atr14": [1.0] * 40,
            "rsi14": [50.0] * 40,
            "volume_ratio20": [1.2] * 40,
            "close_position": [0.5] * 40,
            "time": [datetime(2026, 6, 3, 10, 0, tzinfo=UTC)] * 40,
        }
    )
    frame_1h = frame_15m.clone()
    prepared = _prepared(work_15m=frame_15m, work_1h=frame_1h)
    settings = BotSettings(tg_token="test", target_chat_id="1")
    defaults = OrderBlockSetup.DEFAULTS
    effective = dict(defaults)
    _detect_order_block_extended(
        prepared,
        settings,
        defaults,
        effective,
        "order_block",
        "continuation",
    )
    scanned_frame = mock_lob.call_args[0][0]
    assert scanned_frame is prepared.work_15m
    assert scanned_frame is not prepared.work_1h


@patch("bot.strategies.order_block._detect_order_block_extended")
@patch("bot.strategies.order_block.detect_order_block", return_value=None)
def test_order_block_setup_passes_spec_kwargs(_mock_spec, mock_extended) -> None:
    prepared = _prepared()
    settings = BotSettings(tg_token="test", target_chat_id="1")
    defaults = OrderBlockSetup.DEFAULTS
    effective = {**defaults, "ob_max_age": 55.0, "touch_buffer_atr": 0.4}
    mock_extended.return_value = None
    detect_order_block_setup(
        prepared,
        settings,
        defaults,
        effective,
        "order_block",
        "continuation",
    )
    _mock_spec.assert_called_once()
    assert _mock_spec.call_args.kwargs["ob_max_age"] == 55
    assert _mock_spec.call_args.kwargs["touch_buffer_atr"] == pytest.approx(0.4)
