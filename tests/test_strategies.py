from __future__ import annotations

from datetime import UTC, datetime

import polars as pl
import pytest

from bot.config import BotSettings
from bot.models import PreparedSymbol, UniverseSymbol
from bot.strategies import STRATEGY_CLASSES
from bot.strategies.bos_choch import _select_external_stop_level
from bot.strategies.session_killzone import SessionKillzoneSetup, _active_killzone_name


def _prepared_symbol() -> PreparedSymbol:
    now = datetime.now(UTC)
    frame = pl.DataFrame(
        {
            "open_time": [now],
            "close_time": [now],
            "open": [100.0],
            "high": [101.0],
            "low": [99.0],
            "close": [100.0],
            "volume": [1000.0],
            "ema20": [100.0],
            "ema50": [100.0],
            "ema200": [100.0],
            "atr14": [1.0],
            "adx14": [30.0],
            "delta_ratio": [0.5],
            "volume_ratio20": [1.0],
        }
    )
    universe = UniverseSymbol(
        symbol="BTCUSDT",
        base_asset="BTC",
        quote_asset="USDT",
        contract_type="PERPETUAL",
        status="TRADING",
        onboard_date_ms=0,
        quote_volume=1_000_000,
        price_change_pct=0.0,
        last_price=100.0,
    )
    return PreparedSymbol(
        universe=universe,
        work_1h=frame,
        work_15m=frame,
        bid_price=99.9,
        ask_price=100.1,
        spread_bps=5.0,
    )


@pytest.mark.parametrize("strategy_cls", STRATEGY_CLASSES)
def test_strategy_metadata_and_calculate_contract(strategy_cls: type) -> None:
    settings = BotSettings(tg_token="1" * 30, target_chat_id="123")
    strategy = strategy_cls(settings=settings)
    result = strategy.calculate(_prepared_symbol())

    assert strategy.metadata.strategy_id
    assert isinstance(strategy.get_optimizable_params(settings), dict)
    assert result.setup_id == strategy.metadata.strategy_id


def test_session_killzone_rejects_empty_15m_before_time_lookup() -> None:
    settings = BotSettings(tg_token="1" * 30, target_chat_id="123")
    prepared = _prepared_symbol()
    prepared.work_15m = pl.DataFrame()

    result = SessionKillzoneSetup(settings=settings).calculate(prepared)

    assert result.signal is None
    assert result.decision.reason_code.endswith("insufficient_15m_bars")


def test_session_killzone_overlap_is_first_class_window() -> None:
    assert _active_killzone_name(14) == "Overlap"
    assert _active_killzone_name(8) == "London"
    assert _active_killzone_name(1) == "Asia"
    assert _active_killzone_name(23) is None


def test_session_killzone_supports_configured_cross_midnight_window() -> None:
    params = {
        "overlap_start_hour_utc": 22,
        "overlap_end_hour_utc": 2,
        "london_start_hour_utc": 7,
        "london_end_hour_utc": 10,
        "ny_start_hour_utc": 13,
        "ny_end_hour_utc": 16,
        "asia_start_hour_utc": 0,
        "asia_end_hour_utc": 0,
    }

    assert _active_killzone_name(23, params) == "Overlap"
    assert _active_killzone_name(1, params) == "Overlap"
    assert _active_killzone_name(3, params) is None


def test_bos_choch_external_stop_selector_reports_side_filtering() -> None:
    level, details = _select_external_stop_level(
        markers=[1.0, 1.0, -1.0],
        levels=[99.0, 101.0, 98.0],
        search_end=2,
        marker=1.0,
        price=100.0,
        above_price=True,
    )

    assert level == 101.0
    assert details["external_marker_candidates"] == 1
    assert details["external_side_filtered"] == 0
    assert details["external_selected_index"] == 1


def test_bos_choch_external_stop_selector_diagnoses_missing_anchor() -> None:
    level, details = _select_external_stop_level(
        markers=[1.0, 1.0],
        levels=[98.0, 99.0],
        search_end=1,
        marker=1.0,
        price=100.0,
        above_price=True,
    )

    assert level is None
    assert details["external_marker_candidates"] == 2
    assert details["external_side_filtered"] == 2
