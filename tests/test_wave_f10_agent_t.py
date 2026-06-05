"""Wave F10 Agent T - run metadata, prune, buffer slim fields, metrics, reconcile."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from bot.cli import prune_run_dirs
from bot.dashboard.app import BotDashboard
from bot.dashboard.live import DashboardLiveData
from bot.domain.schemas import PipelineResult
from bot.runtime.bot import SignalBot
from bot.runtime.delivery_orchestrator import DeliveryOrchestrator
from bot.runtime.telemetry_manager import TelemetryManager
from bot.telemetry import (
    TelemetryStore,
    apply_slim_message_buffer,
    run_dir_started_at,
    slim_message_buffer_fields,
)


def test_finalize_run_metadata_writes_ended_at_and_totals(tmp_path: Path) -> None:
    store = TelemetryStore(tmp_path / "telemetry", run_id="run_close_1")
    store.append_jsonl(
        "cycles.jsonl",
        {"candidate_count": 2, "delivery_success_count": 1, "rejected_count": 0},
    )
    store.append_jsonl(
        "delivery.jsonl",
        {"delivery_status": "sent", "symbol": "BTCUSDT"},
    )
    totals = store.collect_session_totals(extras={"session_action_delivered": 1})
    store.finalize_run_metadata(session_totals=totals)

    metadata = json.loads((store.base_dir / "run_metadata.json").read_text(encoding="utf-8"))
    assert metadata["run_id"] == "run_close_1"
    assert metadata["ended_at"]
    assert metadata["session_totals"]["cycles"] == 1
    assert metadata["session_totals"]["delivered_cycles"] == 1
    assert metadata["session_totals"]["delivery_success"] == 1
    assert metadata["session_totals"]["session_action_delivered"] == 1


@pytest.mark.asyncio
async def test_bot_close_finalizes_run_metadata(tmp_path: Path) -> None:

    run_id = "run_bot_close"
    telemetry = TelemetryStore(tmp_path / "telemetry", run_id=run_id)
    bot = MagicMock(spec=SignalBot)
    bot.telemetry = telemetry
    bot._session_action_delivered = 2
    bot._signal_diagnostics = None
    bot._ws_manager = None
    bot._background_tasks = set()
    bot._shutdown = MagicMock()
    bot._shutdown.is_set = MagicMock(return_value=True)
    bot._running = True
    bot.tracker = MagicMock()
    bot.tracker.persist_tracking_state = MagicMock(return_value=None)
    bot.dashboard = MagicMock()
    bot.dashboard.stop_server_async = AsyncMock()
    bot.delivery = MagicMock()
    bot.delivery.close = AsyncMock()
    bot.alerts = MagicMock()
    bot.alerts.close = AsyncMock()
    bot._modern_repo = MagicMock()
    bot._modern_repo.close = AsyncMock()
    bot._get_spot_refresh_runner = MagicMock(return_value=MagicMock(close=AsyncMock()))
    bot.client = MagicMock()
    bot.client.close = AsyncMock()
    bot.telegram = MagicMock()
    bot.telegram.close = AsyncMock()

    await SignalBot.close(bot)

    metadata = json.loads((telemetry.base_dir / "run_metadata.json").read_text(encoding="utf-8"))
    assert metadata["ended_at"]
    assert metadata["session_totals"]["session_action_delivered"] == 2


def test_prune_run_dirs_uses_metadata_started_at_not_mtime(tmp_path: Path) -> None:
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    old_run = runs_dir / "old_run"
    new_run = runs_dir / "new_run"
    old_run.mkdir()
    new_run.mkdir()

    now = datetime(2026, 6, 3, 12, 0, 0, tzinfo=UTC)
    (old_run / "run_metadata.json").write_text(
        json.dumps({"run_id": "old_run", "started_at": (now - timedelta(days=40)).isoformat()}),
        encoding="utf-8",
    )
    (new_run / "run_metadata.json").write_text(
        json.dumps({"run_id": "new_run", "started_at": (now - timedelta(days=1)).isoformat()}),
        encoding="utf-8",
    )
    # Fresh mtime on old_run would normally protect it from mtime-based pruning.
    old_touch = (now + timedelta(days=1)).timestamp()

    os.utime(old_run, (old_touch, old_touch))

    removed = prune_run_dirs(runs_dir, keep=1, retention_days=30, now=now)
    assert removed == 1
    assert not old_run.exists()
    assert new_run.exists()


def test_calibration_snapshots_live_under_run_dir(tmp_path: Path) -> None:
    store = TelemetryStore(tmp_path / "telemetry", run_id="run_cal")
    store.append_calibration_snapshot("BTCUSDT", {"funding_rate": 0.0001})
    path = store.base_dir / "calibration_snapshots.jsonl"
    assert path.exists()
    row = json.loads(path.read_text(encoding="utf-8").strip())
    assert row["symbol"] == "BTCUSDT"


def test_apply_slim_message_buffer_promotes_top_level_fields() -> None:
    row = {
        "message_buffer": {"size": 7, "dropped": 3},
        "buffer_message_count": 99,
    }
    apply_slim_message_buffer(row)
    assert row["message_buffer_size"] == 7
    assert row["message_buffer_dropped"] == 3
    assert "message_buffer" not in row


def test_telemetry_manager_cycles_row_has_slim_buffer_fields() -> None:
    bot = MagicMock()
    bot._shortlist_source = "test"
    bot._prepare_error_count = 0
    bot._ws_manager = MagicMock()
    bot._ws_manager.state_snapshot.return_value = {
        "message_buffer": {"size": 4, "dropped": 2},
        "ws_connected": True,
    }
    bot._bus = None
    bot.client = MagicMock()
    bot.client.state_snapshot.return_value = {}
    bot.telemetry = MagicMock()

    manager = TelemetryManager(bot)
    result = PipelineResult(
        symbol="BTCUSDT",
        trigger="kline_close",
        event_ts=datetime.now(UTC),
        raw_setups=1,
        status="ok",
        funnel={"delivery_status_counts": {"sent": 1}},
    )
    manager.emit_cycle_log(
        symbol="BTCUSDT",
        interval="15m",
        event_ts=datetime.now(UTC),
        shortlist_size=1,
        tracking_events=[],
        result=result,
        candidates=[],
        rejected=[],
        delivered=[],
    )
    cycle_calls = [
        call
        for call in bot.telemetry.append_jsonl.call_args_list
        if call.args and call.args[0] == "cycles.jsonl"
    ]
    assert cycle_calls
    row = cycle_calls[-1].args[1]
    assert row["message_buffer_size"] == 4
    assert row["message_buffer_dropped"] == 2
    assert "message_buffer" not in row


def test_runtime_endpoint_exposes_slim_buffer_fields() -> None:
    live = DashboardLiveData(lambda: None)
    bot = SimpleNamespace(
        _ws_manager=SimpleNamespace(
            state_snapshot=lambda: {"message_buffer": {"size": 11, "dropped": 5}}
        ),
        settings=SimpleNamespace(runtime=SimpleNamespace()),
        _signal_diagnostics=None,
    )
    live._bot_getter = lambda: bot
    live._iter_recent = lambda *_args, **_kwargs: []  # type: ignore[method-assign]

    payload = live._runtime_uncached()
    assert payload["message_buffer_size"] == 11
    assert payload["message_buffer_dropped"] == 5


def test_slim_message_buffer_fields_helper() -> None:
    assert slim_message_buffer_fields({"message_buffer": {"size": 2, "dropped": 1}}) == {
        "message_buffer_size": 2,
        "message_buffer_dropped": 1,
    }


def test_delivery_orchestrator_records_metrics_on_success() -> None:
    bot = MagicMock()
    bot.metrics = MagicMock()
    signal = MagicMock(setup_id="ema_bounce", direction="long")
    DeliveryOrchestrator(bot)._record_metrics_delivered(signal)
    bot.metrics.record_signal_delivered.assert_called_once_with("ema_bounce", "long")


def test_delivery_orchestrator_records_metrics_on_reject() -> None:
    bot = MagicMock()
    bot.metrics = MagicMock()
    signal = MagicMock(setup_id="rsi_div", direction="short")
    DeliveryOrchestrator(bot)._record_metrics_rejected(signal, "delivery_failed")
    bot.metrics.record_signal_rejected.assert_called_once_with(
        "rsi_div", "short", "delivery_failed"
    )


def test_funnel_reconcile_matches_delivery_success(tmp_path: Path) -> None:
    telemetry_root = tmp_path / "telemetry"
    analysis = telemetry_root / "runs" / "run_rec" / "analysis"
    analysis.mkdir(parents=True)
    (analysis / "cycles.jsonl").write_text(
        "\n".join(
            [
                json.dumps({"delivery_success_count": 2}),
                json.dumps({"delivery_success_count": 1}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    (analysis / "delivery.jsonl").write_text(
        "\n".join(
            [
                json.dumps({"delivery_status": "sent"}),
                json.dumps({"delivery_status": "logged"}),
                json.dumps({"delivery_status": "failed"}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    bot = SimpleNamespace(
        settings=SimpleNamespace(telemetry_dir=telemetry_root),
        telemetry=SimpleNamespace(run_id="run_rec"),
    )
    live = DashboardLiveData(lambda: bot)
    payload = live.funnel_reconcile(max_rows=10_000)

    assert payload["cycles_delivery_success_total"] == 3
    assert payload["delivery_jsonl_success_total"] == 2
    assert payload["delta"] == 1
    assert payload["match"] is False


def test_dashboard_funnel_reconcile_route_registered() -> None:
    dashboard = BotDashboard(
        bot=SimpleNamespace(
            settings=SimpleNamespace(
                telemetry_dir=Path("/tmp/unused"),
                runtime=SimpleNamespace(dashboard_allow_origins=["http://127.0.0.1"]),
            ),
            telemetry=SimpleNamespace(run_id=None),
        ),
        port=18080,
    )
    assert dashboard.app is not None
    paths = [getattr(route, "path", "") for route in dashboard.app.routes]
    assert "/api/live/funnel/reconcile" in paths


def test_run_dir_started_at_prefers_metadata(tmp_path: Path) -> None:
    run_dir = tmp_path / "run_meta"
    run_dir.mkdir()
    started = datetime(2026, 1, 1, tzinfo=UTC)
    (run_dir / "run_metadata.json").write_text(
        json.dumps({"started_at": started.isoformat()}),
        encoding="utf-8",
    )
    assert run_dir_started_at(run_dir) == started
