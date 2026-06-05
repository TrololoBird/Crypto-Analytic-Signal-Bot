"""Wave F9 Agent R - journal normalization, diary analytics, outcome classification."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from bot.persistence.db_status import (
    DbStatusSummary,
    collect_db_status_from_conn,
    format_db_status_html,
)
from bot.persistence.diary_store import DiaryStore
from bot.persistence.journal import build_journal_report, normalize_tracking_event
from bot.persistence.outcomes import aggregate_setup_stats, classify_outcome_result
from bot.persistence.repository.memory import MemoryRepository

if TYPE_CHECKING:
    from pathlib import Path


def test_normalize_tracking_event_maps_runtime_variants() -> None:
    assert normalize_tracking_event("tp1_hit") == "tp1"
    assert normalize_tracking_event("TP2_HIT") == "tp2"
    assert normalize_tracking_event("stop_loss") == "sl"
    assert normalize_tracking_event("expired_pending") == "expired"
    assert normalize_tracking_event("activated") is None
    assert normalize_tracking_event("") is None


def test_build_journal_report_counts_event_type_aliases(tmp_path: Path) -> None:
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
            "tracking_ref": "B",
        },
        {
            "setup_id": "fvg_setup",
            "event_type": "expired_active",
            "tracking_ref": "C",
        },
    ]
    (analysis / "tracking_events.jsonl").write_text(
        "\n".join(json.dumps(row) for row in events),
        encoding="utf-8",
    )
    report = build_journal_report(tmp_path)
    assert report.setup_outcomes["order_block"] == {"tp1": 1, "sl": 1}
    assert report.setup_outcomes["fvg_setup"] == {"expired": 1}


def test_classify_outcome_result_win_loss_neutral() -> None:
    assert classify_outcome_result("tp1_hit") == "win"
    assert classify_outcome_result("stop_loss") == "loss"
    assert classify_outcome_result("expired_pending") == "neutral"
    assert classify_outcome_result("trailing_stop", was_profitable=True) == "win"
    assert classify_outcome_result("trailing_stop", pnl_r_multiple=-0.5) == "loss"
    assert classify_outcome_result("ambiguous_exit", pnl_r_multiple=0.0) == "neutral"
    assert (
        classify_outcome_result("breakeven_stop", was_profitable=False, activated_at="2026-01-01")
        == "loss"
    )


def test_aggregate_setup_stats_uses_win_loss_denominator() -> None:
    rows = [
        {
            "setup_id": "ema_bounce",
            "result": "tp1_hit",
            "was_profitable": 1,
            "pnl_r_multiple": 1.2,
            "pnl_pct": 2.0,
            "activated_at": "2026-01-01",
        },
        {
            "setup_id": "ema_bounce",
            "result": "stop_loss",
            "was_profitable": 0,
            "pnl_r_multiple": -1.0,
            "pnl_pct": -1.5,
            "activated_at": "2026-01-02",
        },
        {
            "setup_id": "ema_bounce",
            "result": "expired_pending",
            "was_profitable": 0,
            "pnl_r_multiple": 0.0,
            "pnl_pct": 0.0,
            "activated_at": None,
        },
    ]
    stats = aggregate_setup_stats(rows)
    assert len(stats) == 1
    row = stats[0]
    assert row["wins"] == 1
    assert row["losses"] == 1
    assert row["total"] == 2
    assert row["win_rate"] == 0.5


@pytest.mark.asyncio
async def test_diary_get_analytics_null_safe_when_no_wins_losses(tmp_path: Path) -> None:
    db_path = tmp_path / "bot.db"
    repo = MemoryRepository(db_path, tmp_path / "data")
    await repo.initialize()
    await repo.close()

    store = DiaryStore(db_path)
    await store.initialize()
    now = datetime.now(UTC).isoformat()
    await store.create_trade(
        {
            "entry_price": 100.0,
            "entry_time": now,
            "exit_price": 99.0,
            "exit_time": now,
            "exit_reason": "manual",
            "pnl_percent": 1.0,
            "pnl_usd": 10.0,
        }
    )
    analytics = await store.get_analytics(days=30)
    summary = analytics["summary"]
    assert summary["total_trades"] == 1
    assert summary["closed_trades"] == 0
    assert summary["win_rate"] is None
    assert summary["avg_pnl_percent"] is None
    assert summary["avg_pnl_usd"] is None
    assert analytics["calendar"][0]["wins"] == 0
    assert analytics["calendar"][0]["losses"] == 0
    assert analytics["calendar"][0]["pnl_usd"] == 10.0


@pytest.mark.asyncio
async def test_diary_get_analytics_win_rate_from_wins_and_losses(tmp_path: Path) -> None:
    db_path = tmp_path / "bot.db"
    repo = MemoryRepository(db_path, tmp_path / "data")
    await repo.initialize()
    await repo.close()

    store = DiaryStore(db_path)
    await store.initialize()
    now = datetime.now(UTC).isoformat()
    for exit_reason, pnl in (("tp1", 2.0), ("tp2", 3.0), ("sl", -1.0)):
        await store.create_trade(
            {
                "entry_price": 100.0,
                "entry_time": now,
                "exit_price": 101.0,
                "exit_time": now,
                "exit_reason": exit_reason,
                "pnl_percent": pnl,
                "pnl_usd": pnl,
            }
        )
    summary = (await store.get_analytics(days=30))["summary"]
    assert summary["closed_trades"] == 3
    assert summary["win_rate"] == round(2 / 3, 4)
    assert summary["avg_pnl_percent"] == round((2.0 + 3.0 - 1.0) / 3, 2)


@pytest.mark.asyncio
async def test_collect_db_status_includes_signal_outcome_counts() -> None:
    class _Cursor:
        def __init__(self, rows: list[tuple[str, int]]) -> None:
            self._rows = rows

        async def fetchall(self) -> list[tuple[str, int]]:
            return self._rows

        async def fetchone(self) -> tuple[int] | None:
            return (1,)

    class _Conn:
        async def execute(self, sql: str) -> _Cursor:
            if "active_signals" in sql:
                return _Cursor([("active", 2)])
            if "sqlite_master" in sql and "signal_outcomes" in sql:
                return _Cursor([])
            if "signal_outcomes" in sql:
                return _Cursor([("tp1_hit", 4), ("stop_loss", 2)])
            msg = f"unexpected sql: {sql}"
            raise AssertionError(msg)

    conn = _Conn()
    with patch(
        "bot.persistence.db_status.fetch_schema_version_rows",
        return_value=[(5, "test", "2026-01-01")],
    ):
        summary = await collect_db_status_from_conn(conn)  # type: ignore[arg-type]

    assert summary.outcome_counts == {"stop_loss": 2, "tp1_hit": 4}
    assert summary.outcomes_total == 6


def test_format_db_status_html_includes_outcome_counts() -> None:
    summary = DbStatusSummary(
        migration_version=5,
        signal_counts={"active": 1},
        outcome_counts={"tp1_hit": 3, "stop_loss": 1},
    )
    text = format_db_status_html(summary)
    assert "Outcomes:" in text
    assert "tp1_hit" in text
    assert "stop_loss" in text
