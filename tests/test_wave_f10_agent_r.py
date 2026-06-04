"""Wave F10 Agent R — journal repo primary, dedup, migration v6, SL aliases, outcome queries."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pytest

from bot.migrations import fetch_schema_version, migrate_db
from bot.persistence.diary_store import DiaryStore
from bot.persistence.journal import (
    build_journal_report,
    build_journal_report_from_repo,
    build_journal_report_primary,
)
from bot.persistence.repository.memory import MemoryRepository
from bot.persistence.repository.queries.outcomes import (
    fetch_setup_stats_rows,
    fetch_signal_outcome_rows,
)
from bot.persistence.sl_diagnostics import classify_stop_loss_root_cause, reclassify_sl_outcomes


def _outcome_row(
    *,
    tracking_id: str,
    tracking_ref: str,
    setup_id: str,
    result: str,
    created_at: str = "2026-06-03T10:00:00+00:00",
) -> dict:
    return {
        "tracking_id": tracking_id,
        "signal_id": tracking_id,
        "tracking_ref": tracking_ref,
        "symbol": "BTCUSDT",
        "setup_id": setup_id,
        "direction": "long",
        "timeframe": "15m",
        "created_at": created_at,
        "closed_at": created_at,
        "result": result,
        "features": {},
    }


@pytest.mark.asyncio
async def test_build_journal_report_from_repo_aggregates_outcomes(tmp_path: Path) -> None:
    repo = MemoryRepository(tmp_path / "bot.db", tmp_path / "data")
    await repo.initialize()
    await repo.increment_tracking_stats(signals_sent=2)
    await repo.save_signal_outcomes_batch(
        [
            _outcome_row(
                tracking_id="t1",
                tracking_ref="REF-1",
                setup_id="order_block",
                result="tp1_hit",
            ),
            _outcome_row(
                tracking_id="t2",
                tracking_ref="REF-2",
                setup_id="order_block",
                result="stop_loss",
            ),
        ]
    )
    report = await build_journal_report_from_repo(repo)
    await repo.close()

    assert report.signals_sent == 2
    assert report.setup_outcomes["order_block"] == {"tp1": 1, "sl": 1}


def test_build_journal_report_dedups_terminal_outcome_per_tracking_ref(tmp_path: Path) -> None:
    analysis = tmp_path / "analysis"
    analysis.mkdir()
    events = [
        {
            "setup_id": "order_block",
            "event_type": "tp1_hit",
            "tracking_ref": "A",
        },
        {
            "setup_id": "order_block",
            "event_type": "stop_loss",
            "tracking_ref": "A",
        },
        {
            "setup_id": "fvg_setup",
            "event_type": "expired_active",
            "tracking_ref": "B",
        },
    ]
    (analysis / "tracking_events.jsonl").write_text(
        "\n".join(json.dumps(row) for row in events),
        encoding="utf-8",
    )
    report = build_journal_report(tmp_path)
    assert report.setup_outcomes["order_block"] == {"tp1": 1}
    assert report.setup_outcomes["fvg_setup"] == {"expired": 1}


@pytest.mark.asyncio
async def test_build_journal_report_primary_warns_on_parity_mismatch(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    telemetry = tmp_path / "telemetry"
    analysis = telemetry / "analysis"
    analysis.mkdir(parents=True)
    (analysis / "selected.jsonl").write_text(
        json.dumps({"ts": "2026-06-03T12:00:00+00:00", "setup_id": "order_block"}) + "\n",
        encoding="utf-8",
    )

    repo = MemoryRepository(tmp_path / "bot.db", tmp_path / "data")
    await repo.initialize()
    await repo.increment_tracking_stats(signals_sent=3)
    report = await build_journal_report_primary(telemetry, repo)
    await repo.close()

    assert report.signals_sent == 3
    assert any("parity mismatch" in record.message for record in caplog.records)


@pytest.mark.asyncio
async def test_migration_v6_adds_trader_diary_symbol_index(tmp_path: Path) -> None:
    repo = MemoryRepository(tmp_path / "bot.db", tmp_path / "data")
    await repo.initialize()
    conn = repo._require_conn()
    try:
        version_before = await fetch_schema_version(conn)
        applied = await migrate_db(conn)
        version_after = await fetch_schema_version(conn)
        async with conn.execute("PRAGMA table_info(trader_diary)") as cursor:
            columns = {str(row[1]) for row in await cursor.fetchall()}
        async with conn.execute(
            """
            SELECT name FROM sqlite_master
            WHERE type='index' AND name='idx_diary_symbol'
            """
        ) as cursor:
            index_row = await cursor.fetchone()
    finally:
        await repo.close()

    assert "symbol" in columns
    assert index_row is not None
    assert version_after >= 6
    assert applied >= 0 or version_before >= 6


@pytest.mark.asyncio
async def test_diary_create_trade_persists_symbol(tmp_path: Path) -> None:
    db_path = tmp_path / "bot.db"
    repo = MemoryRepository(db_path, tmp_path / "data")
    await repo.initialize()
    await repo.close()

    store = DiaryStore(db_path)
    await store.initialize()
    trade = await store.create_trade(
        {
            "symbol": "ETHUSDT",
            "entry_price": 100.0,
            "entry_time": datetime.now(UTC).isoformat(),
        }
    )
    assert trade["symbol"] == "ETHUSDT"


def test_classify_stop_loss_root_cause_feature_aliases() -> None:
    diag = classify_stop_loss_root_cause(
        direction="long",
        mfe=0.2,
        mae=1.0,
        time_to_entry_min=5,
        time_to_exit_min=20,
        features={"atr_pct_15m": 2.0, "base_score": 0.50},
    )
    assert diag["code"] == "wide_volatility_stop"
    assert "elevated_atr_volatility" in diag["reasons"]
    assert "low_entry_score" in diag["reasons"]


def test_reclassify_sl_outcomes_updates_features() -> None:
    rows = [
        {
            "result": "stop_loss",
            "direction": "long",
            "mfe": 0.0,
            "mae": 1.0,
            "time_to_entry_min": 2,
            "time_to_exit_min": 8,
            "features": {"market_regime": "bear", "atr_pct_15m": 2.0, "base_score": 0.50},
        },
        {
            "result": "tp1_hit",
            "direction": "long",
            "mfe": 1.0,
            "mae": 0.1,
            "time_to_entry_min": 2,
            "time_to_exit_min": 20,
            "features": {},
        },
    ]
    updated = reclassify_sl_outcomes(rows)
    assert updated[0]["features"]["sl_root_cause"] == "bear_long_immediate_stop"
    assert "sl_diagnostics" in updated[0]["features"]
    assert updated[1]["features"] == {}


@pytest.mark.asyncio
async def test_outcome_queries_match_repository_methods(tmp_path: Path) -> None:
    repo = MemoryRepository(tmp_path / "bot.db", tmp_path / "data")
    await repo.initialize()
    await repo.save_signal_outcomes_batch(
        [
            _outcome_row(
                tracking_id="t1",
                tracking_ref="REF-1",
                setup_id="ema_bounce",
                result="tp1_hit",
            ),
            _outcome_row(
                tracking_id="t2",
                tracking_ref="REF-2",
                setup_id="ema_bounce",
                result="stop_loss",
            ),
        ]
    )
    conn = repo._require_conn()
    raw_rows = await fetch_setup_stats_rows(conn, setup_id="ema_bounce", last_days=None)
    outcome_rows = await fetch_signal_outcome_rows(conn, setup_id="ema_bounce", last_days=None)
    setup_stats = await repo.get_setup_stats(setup_id="ema_bounce", last_days=None)
    repo_outcomes = await repo.get_signal_outcomes(setup_id="ema_bounce", last_days=None)
    await repo.close()

    assert len(raw_rows) == 2
    assert len(outcome_rows) == 2
    assert setup_stats[0]["wins"] == 1
    assert setup_stats[0]["losses"] == 1
    assert {row["tracking_ref"] for row in repo_outcomes} == {"REF-1", "REF-2"}


@pytest.mark.asyncio
async def test_get_signal_outcomes_delegates_to_query_module(tmp_path: Path) -> None:
    repo = MemoryRepository(tmp_path / "bot.db", tmp_path / "data")
    await repo.initialize()
    with patch(
        "bot.persistence.repository.memory.fetch_signal_outcome_rows",
        wraps=fetch_signal_outcome_rows,
    ) as fetch_mock:
        await repo.get_signal_outcomes(last_days=7, limit=5)
    await repo.close()
    fetch_mock.assert_awaited_once()
