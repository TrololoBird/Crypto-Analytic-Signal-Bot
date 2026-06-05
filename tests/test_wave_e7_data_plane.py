"""Wave E7: UTC session CVD reset and calibration basis warm."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import polars as pl
import pytest

from bot.features.prepare_frame import add_session_cvd
from bot.strategies._common import with_spec_columns


def _two_day_flow_frame() -> pl.DataFrame:
    times = [
        datetime(2026, 6, 1, 23, 45, tzinfo=UTC),
        datetime(2026, 6, 2, 0, 0, tzinfo=UTC),
    ]
    return pl.DataFrame(
        {
            "open_time": times,
            "close_time": times,
            "open": [1.0, 1.0],
            "high": [1.0, 1.0],
            "low": [1.0, 1.0],
            "close": [1.0, 1.0],
            "volume": [100.0, 100.0],
            "taker_buy_base_volume": [70.0, 70.0],
            "delta_ratio": [0.7, 0.7],
        }
    )


def test_session_cvd_resets_on_utc_date_boundary() -> None:
    out = add_session_cvd(_two_day_flow_frame())
    cvd = out["session_cvd"].to_list()
    assert cvd[0] == pytest.approx(40.0)
    assert cvd[1] == pytest.approx(40.0)


def test_spec_cvd_uses_session_cvd_column() -> None:
    frame = add_session_cvd(_two_day_flow_frame())
    work = with_spec_columns(frame)
    assert work["spec_cvd"].to_list() == frame["session_cvd"].to_list()


@pytest.mark.asyncio
async def test_live_shortlist_fit_counts_warms_basis_when_requested() -> None:
    from scripts.strategy_shortlist_matrix import live_shortlist_fit_counts

    fake_shortlist = [MagicMock(symbol="BTCUSDT"), MagicMock(symbol="ETHUSDT")]
    fake_summary = {"strategy_fit_counts": {"cvd_divergence": 2}, "gate_passed": 2}
    client = AsyncMock()
    client.fetch_exchange_symbols.return_value = []
    client.fetch_ticker_24h.return_value = []
    client.fetch_basis.return_value = 0.01
    client.close.return_value = None

    with (
        patch(
            "scripts.strategy_shortlist_matrix.BinanceClientImpl",
            return_value=client,
        ),
        patch(
            "scripts.strategy_shortlist_matrix.build_shortlist",
            return_value=(fake_shortlist, fake_summary),
        ),
        patch(
            "scripts.strategy_shortlist_matrix.load_settings",
            return_value=MagicMock(network={}, ws=MagicMock(rest_timeout_seconds=10.0)),
        ),
    ):
        result = await live_shortlist_fit_counts(
            MagicMock(),
            include_basis=True,
            basis_warm_limit=2,
        )

    assert result["basis_warm"]["basis_warm_attempted"] == 2
    assert result["basis_warm"]["basis_warm_ok"] == 2
    assert client.fetch_basis.await_count == 4


def test_nightly_calibration_includes_basis_by_default() -> None:
    from pathlib import Path

    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=Path("config.toml"))
    parser.add_argument("--symbols", type=int, default=25)
    parser.add_argument(
        "--output", type=Path, default=Path("data/bot/reports/nightly_calibration.json")
    )
    parser.add_argument("--no-include-basis", action="store_true")
    args = parser.parse_args([])
    cmd: list[str] = ["--live-shortlist", "--json"]
    if not args.no_include_basis:
        cmd.extend(["--include-basis", "--basis-warm-limit", str(args.symbols)])

    assert "--include-basis" in cmd
    assert cmd[cmd.index("--basis-warm-limit") + 1] == "25"

    args_off = parser.parse_args(["--no-include-basis"])
    cmd_off: list[str] = []
    if not args_off.no_include_basis:
        cmd_off.append("--include-basis")
    assert "--include-basis" not in cmd_off
