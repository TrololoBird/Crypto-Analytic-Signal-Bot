"""Wave E8 agent B: extended detect, defaults drift, schedule, OI, absorption."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import polars as pl
import pytest

from bot.domain.config import BotSettings
from bot.domain.schemas import PreparedSymbol, UniverseSymbol
from bot.setups.spec_runtime import run_setup_detection
from bot.strategies._common import SpecHit
from bot.strategies.absorption import AbsorptionSetup, detect_absorption, detect_absorption_prepared
from bot.strategies.oi_divergence import (
    OIDivergenceSetup,
    _oi_divergence_price_change,
    detect_oi_divergence,
)
from bot.strategies.order_block import OrderBlockSetup, detect_order_block_setup
from bot.strategies.session_killzone import SessionKillzoneSetup, detect_session_killzone
from scripts.reconcile_strategy_defaults import collect_defaults_drift


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
        last_price=100.0,
    )


def _settings() -> BotSettings:
    return BotSettings(tg_token="test", target_chat_id="1")


def _prepared(**overrides: object) -> PreparedSymbol:
    frame = pl.DataFrame(
        {
            "open": [100.0] * 30,
            "high": [101.0] * 30,
            "low": [99.0] * 30,
            "close": [100.0] * 30,
            "volume": [1000.0] * 30,
            "atr14": [1.0] * 30,
            "rsi14": [50.0] * 30,
            "volume_ratio20": [1.2] * 30,
            "close_position": [0.5] * 30,
            "time": [datetime(2026, 6, 3, 10, 0, tzinfo=UTC)] * 30,
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
        mark_price=100.0,
    )
    for key, value in overrides.items():
        setattr(base, key, value)
    return base


def test_run_setup_detection_invokes_extended_when_spec_misses() -> None:
    prepared = _prepared()
    calls: list[str] = []

    def _spec(_frame: pl.DataFrame, *, timeframe: str = "15m") -> SpecHit | None:
        calls.append(f"spec:{timeframe}")
        return None

    def _extended(*_args: object, **_kwargs: object) -> None:
        calls.append("extended")
        return None

    run_setup_detection(
        prepared=prepared,
        settings=SimpleNamespace(),
        setup_id="order_block",
        family="continuation",
        defaults={"base_score": 0.52},
        effective={"base_score": 0.52},
        spec_detect=_spec,
        extended_detect=_extended,
    )
    assert calls == ["spec:15m", "extended"]


def test_order_block_setup_wires_extended_detect() -> None:
    assert OrderBlockSetup.detect_setup is detect_order_block_setup


def test_reconcile_defaults_detects_order_block_drift() -> None:
    rows = [
        row
        for row in collect_defaults_drift(config_dir=Path("config/strategies"))
        if row.field == "base_score"
    ]
    order_block = next(row for row in rows if row.setup_id == "order_block")
    assert order_block.toml_value is not None
    assert order_block.code_value is not None
    assert 0.3 <= order_block.toml_value <= 0.9, (
        f"toml base_score {order_block.toml_value} out of plausible range"
    )
    assert 0.3 <= order_block.code_value <= 0.9, (
        f"code base_score {order_block.code_value} out of plausible range"
    )
    assert order_block.toml_value != order_block.code_value
    assert order_block.status == "drift"


def test_session_killzone_rejects_schedule_inactive() -> None:
    prepared = _prepared()
    params = SessionKillzoneSetup.DEFAULTS.copy()
    params.update(
        {
            "overlap_start_hour_utc": 20,
            "overlap_end_hour_utc": 21,
            "london_start_hour_utc": 20,
            "london_end_hour_utc": 21,
            "ny_start_hour_utc": 20,
            "ny_end_hour_utc": 21,
            "asia_start_hour_utc": 20,
            "asia_end_hour_utc": 21,
        }
    )
    with patch("bot.strategies.session_killzone._reject") as reject:
        result = detect_session_killzone(
            prepared,
            _settings(),
            params,
            setup_id="session_killzone",
            family="breakout",
        )
    assert result is None
    reject.assert_called_once()
    assert reject.call_args.args[2] == "schedule_inactive"


def test_base_setup_schedule_wrapper_rejects_inactive() -> None:
    prepared = _prepared()
    setup = SessionKillzoneSetup(settings=_settings())
    with patch.object(setup, "is_active_now", return_value=False):
        result = setup.calculate(prepared)
    assert result.signal is None
    assert result.decision.reason_code == "context.schedule_inactive"


def test_oi_divergence_prefers_4h_price_change() -> None:
    work_4h = pl.DataFrame({"close": [100.0, 110.0]})
    work_15m = pl.DataFrame({"close": [100.0] * 10})
    prepared = _prepared(work_4h=work_4h, work_15m=work_15m)
    assert _oi_divergence_price_change(prepared) == pytest.approx(10.0)


def test_oi_divergence_signal_uses_prepared_oi_change() -> None:
    work_4h = pl.DataFrame({"close": [100.0, 110.0]})
    prepared = _prepared(work_4h=work_4h, oi_change_pct=0.02)
    params = {
        **OIDivergenceSetup.DEFAULTS,
        "min_abs_oi_change_pct": 0.005,
        "min_price_change_pct": 0.06,
    }
    signal = detect_oi_divergence(
        prepared,
        _settings(),
        params,
        setup_id="oi_divergence",
        family="sentiment",
    )
    assert signal is not None
    assert any("oi_change=0.02" in reason for reason in signal.reasons)


def test_absorption_spec_skips_ohlcv_proxy() -> None:
    frame = pl.DataFrame(
        {
            "open": [100.0] * 30,
            "high": [101.0] * 30,
            "low": [99.0] * 30,
            "close": [100.0] * 30,
            "volume": [1000.0] * 30,
        }
    )
    assert detect_absorption(frame) is None


def test_absorption_rejects_when_agg_trade_missing() -> None:
    prepared = _prepared(agg_trade_delta_30s=None)
    params = AbsorptionSetup.DEFAULTS.copy()
    with patch("bot.strategies.absorption._reject") as reject:
        result = detect_absorption_prepared(
            prepared,
            _settings(),
            params,
            setup_id="absorption",
            family="orderflow",
        )
    assert result is None
    reject.assert_called_once()
    assert reject.call_args.args[2] == "orderflow_missing"
