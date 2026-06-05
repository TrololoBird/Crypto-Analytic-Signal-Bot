"""Wave F9 Agent T — canonical delivery KPIs, run_id stamping, recent signals."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import pytest

from bot.dashboard.app import BotDashboard
from bot.dashboard.live import (
    DashboardLiveData,
    _compute_cycle_totals,
    _compute_session_delta,
    _cycle_delivered_count,
    _delivery_success_rows,
)
from bot.runtime.cycle_runner import CycleRunner
from bot.telemetry import TelemetryStore

if TYPE_CHECKING:
    from pathlib import Path


def test_cycle_totals_prefer_delivery_success_count() -> None:
    cycles = [
        {
            "candidate_count": 4,
            "selected_count": 2,
            "delivered_count": 5,
            "delivery_success_count": 1,
        },
        {
            "candidate_count": 2,
            "selected_count": 1,
            "delivered_count": 3,
            "delivery_success_count": 0,
        },
    ]
    totals = _compute_cycle_totals(cycles)
    delta = _compute_session_delta(cycles)

    assert totals["delivered"] == 1
    assert delta["delivered"] == 1
    assert _cycle_delivered_count(cycles[0]) == 1
    assert _cycle_delivered_count({"delivered_count": 2}) == 2


def test_delivery_success_rows_filters_attempts() -> None:
    rows = [
        {"delivery_status": "sent", "symbol": "BTCUSDT"},
        {"delivery_status": "failed", "symbol": "ETHUSDT"},
        {"status": "logged", "symbol": "SOLUSDT"},
    ]
    success = _delivery_success_rows(rows)
    assert len(success) == 2
    assert {row["symbol"] for row in success} == {"BTCUSDT", "SOLUSDT"}


def test_delivery_uncached_counts_success_not_all_attempts() -> None:
    live = DashboardLiveData(lambda: None)

    def _fake_iter(stem: str, *, max_rows: int, limit_files: int):
        if stem == "delivery":
            yield {"symbol": "BTCUSDT", "setup_id": "ema_bounce", "delivery_status": "sent"}
            yield {"symbol": "ETHUSDT", "setup_id": "rsi_div", "delivery_status": "failed"}
        elif stem == "selected":
            yield {"symbol": "XRPUSDT", "setup_id": "order_block"}

    live._iter_recent = _fake_iter  # type: ignore[method-assign]
    payload = live._delivery_uncached(limit=5)

    assert payload["delivery_count"] == 2
    assert payload["delivery_success_count"] == 1
    assert payload["rows"][0]["source"] == "delivery"
    assert payload["rows"][0]["symbol"] == "BTCUSDT"


def test_overview_session_delivered_uses_delivery_success_count() -> None:
    live = DashboardLiveData(lambda: None)

    def _fake_iter(stem: str, *, max_rows: int, limit_files: int):
        if stem == "cycles":
            yield {
                "candidate_count": 5,
                "selected_count": 2,
                "delivered_count": 99,
                "delivery_success_count": 0,
            }
        elif stem == "delivery":
            yield {"symbol": "BTCUSDT", "setup_id": "ema_bounce", "delivery_status": "sent"}
            yield {"symbol": "ETHUSDT", "setup_id": "rsi_div", "delivery_status": "cooldown"}
        elif stem in {"rejected", "strategy_decisions", "health_runtime", "selected"}:
            return

    live._iter_recent = _fake_iter  # type: ignore[method-assign]
    bot = type(
        "Bot",
        (),
        {
            "_shutdown": type("S", (), {"is_set": lambda self: False})(),
            "_shortlist": [],
            "_shortlist_source": "test",
            "settings": type(
                "Settings", (), {"notifiers": type("N", (), {"provider": "none"})()}
            )(),
        },
    )()
    live._bot_getter = lambda: bot
    overview = live._overview_uncached()

    assert overview["session_delivered"] == 1
    assert overview["last_cycle_delivered"] == 0


def test_telemetry_append_jsonl_stamps_run_id(tmp_path: Path) -> None:
    store = TelemetryStore(tmp_path / "telemetry", run_id="run_test_123")
    store.append_jsonl("cycles.jsonl", {"ts": "2026-01-01T00:00:00+00:00", "candidate_count": 1})
    line = (store.analysis_dir / "cycles.jsonl").read_text(encoding="utf-8").strip()
    row = json.loads(line)
    assert row["run_id"] == "run_test_123"
    assert row["candidate_count"] == 1


def _dashboard_bot_stub(*, telemetry_dir: Path) -> SimpleNamespace:
    return SimpleNamespace(
        settings=SimpleNamespace(
            telemetry_dir=telemetry_dir,
            runtime=SimpleNamespace(dashboard_allow_origins=["http://127.0.0.1"]),
        )
    )


def test_get_recent_signals_prefers_delivery_success(tmp_path: Path) -> None:
    telemetry_root = tmp_path / "telemetry"
    run_dir = telemetry_root / "runs" / "run_a" / "analysis"
    run_dir.mkdir(parents=True)
    delivery_path = run_dir / "delivery.jsonl"
    delivery_path.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "symbol": "BTCUSDT",
                        "setup_id": "ema_bounce",
                        "delivery_status": "failed",
                    }
                ),
                json.dumps(
                    {
                        "symbol": "ETHUSDT",
                        "setup_id": "rsi_div",
                        "delivery_status": "logged",
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    selected_path = run_dir / "selected.jsonl"
    selected_path.write_text(
        json.dumps({"symbol": "BTCUSDT", "setup_id": "ema_bounce"}) + "\n",
        encoding="utf-8",
    )

    dashboard = BotDashboard(bot=_dashboard_bot_stub(telemetry_dir=telemetry_root))
    signals = dashboard._get_recent_signals(limit=5)

    assert len(signals) == 1
    assert signals[0]["symbol"] == "ETHUSDT"
    assert signals[0]["source"] == "delivery"
    assert signals[0]["delivery_status"] == "logged"


def test_get_recent_signals_selected_not_labeled_sent(tmp_path: Path) -> None:
    telemetry_root = tmp_path / "telemetry"
    run_dir = telemetry_root / "runs" / "run_b" / "analysis"
    run_dir.mkdir(parents=True)
    selected_path = run_dir / "selected.jsonl"
    selected_path.write_text(
        json.dumps({"symbol": "BTCUSDT", "setup_id": "ema_bounce"}) + "\n",
        encoding="utf-8",
    )

    dashboard = BotDashboard(bot=_dashboard_bot_stub(telemetry_dir=telemetry_root))
    signals = dashboard._get_recent_signals(limit=5)

    assert len(signals) == 1
    assert signals[0]["source"] == "selected"
    assert signals[0]["delivery_status"] == "selected"


@pytest.mark.asyncio
async def test_emergency_cycle_per_symbol_delivery_status_from_rejects() -> None:
    from collections import Counter
    from datetime import UTC, datetime

    from bot.domain.schemas import PipelineResult

    bot = MagicMock()
    bot.settings.runtime.max_signals_per_cycle = 3
    bot.settings.runtime.emergency_context_warmup_timeout_seconds = 1.0
    bot.settings.runtime.emergency_context_warmup_symbol_limit = 1
    bot.settings.runtime.emergency_context_fetch_timeout_seconds = 1.0
    bot._shortlist = [MagicMock(symbol="BTCUSDT")]
    bot._shortlist_lock = AsyncMock()
    bot._shortlist_lock.__aenter__ = AsyncMock(return_value=None)
    bot._shortlist_lock.__aexit__ = AsyncMock(return_value=None)
    bot.tracker.review_open_signals = AsyncMock(return_value=[])
    bot._deliver_tracking = AsyncMock()
    bot._get_oi_refresh_runner = MagicMock(
        return_value=MagicMock(refresh_once=AsyncMock(return_value=0))
    )

    pipeline_result = PipelineResult(
        symbol="BTCUSDT",
        trigger="emergency_fallback",
        event_ts=datetime.now(UTC),
        raw_setups=0,
        status="ok",
        funnel={},
    )
    bot._fetch_frames = AsyncMock(return_value={})
    bot._ws_cache_enrichments = MagicMock(return_value={})
    bot._run_modern_analysis = AsyncMock(return_value=pipeline_result)

    selected_signal = MagicMock(symbol="BTCUSDT", tracking_id="tid-1")
    selected_signal.to_log_row.return_value = {"symbol": "BTCUSDT", "setup_id": "ema_bounce"}
    bot._select_and_rank.return_value = [selected_signal]
    bot._select_and_deliver = AsyncMock(
        return_value=(
            [],
            [{"symbol": "BTCUSDT", "stage": "delivery", "reason": "delivery_failed"}],
            Counter({"failed": 1}),
            0,
        )
    )
    bot._emit_cycle_log = MagicMock()
    bot.telemetry.append_jsonl = MagicMock()
    bot.last_cycle_summary = {}

    sem = AsyncMock()
    sem.__aenter__ = AsyncMock(return_value=None)
    sem.__aexit__ = AsyncMock(return_value=None)
    bot._analysis_semaphore = sem

    runner = CycleRunner(bot)
    summary = await runner.run_emergency_cycle()

    assert pipeline_result.funnel["delivery_status_counts"] == {"failed": 1}
    assert summary["delivery_status_counts"] == {"failed": 1}
