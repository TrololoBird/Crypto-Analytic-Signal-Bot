from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from bot.core.analyzer.metrics import WinRateCalculator
from bot.core.analyzer.reporter import DailyReporter
from bot.core.memory.repository import MemoryRepository, OutcomeRecord, SignalRecord


UTC = timezone.utc


@pytest.mark.asyncio
async def test_calculate_metrics_respects_until_boundary(tmp_path) -> None:
    repo = MemoryRepository(tmp_path / "repo.db", tmp_path / "data")
    await repo.initialize()
    calc = WinRateCalculator(repo)
    base = datetime(2026, 1, 1, tzinfo=UTC)

    for idx, offset_days in enumerate((0, 2), start=1):
        created = base + timedelta(days=offset_days)
        signal_id = f"sig-{idx}"
        await repo.save_signal(
            SignalRecord(
                signal_id=signal_id,
                symbol="BTCUSDT",
                strategy_id="ema_bounce",
                direction="long",
                entry_price=100.0,
                stop_loss=98.0,
                take_profit_1=103.0,
                take_profit_2=105.0,
                score=0.8,
                created_at=created,
            )
        )
        await repo.save_outcome(
            OutcomeRecord(
                outcome_id=f"out-{idx}",
                signal_id=signal_id,
                symbol="BTCUSDT",
                pnl_24h=2.0 if idx == 1 else -1.0,
                result="win" if idx == 1 else "loss",
                updated_at=created + timedelta(hours=1),
            )
        )

    metrics = await calc.calculate_metrics(
        since=base - timedelta(hours=1),
        until=base + timedelta(days=1),
    )

    assert metrics.total_signals == 1
    assert metrics.wins == 1
    assert metrics.losses == 0
    await repo.close()


@pytest.mark.asyncio
async def test_daily_reporter_populates_top_signals(tmp_path) -> None:
    repo = MemoryRepository(tmp_path / "repo.db", tmp_path / "data")
    await repo.initialize()
    calc = WinRateCalculator(repo)
    reporter = DailyReporter(repo, calc)
    report_day = datetime(2026, 2, 1, 12, 0, tzinfo=UTC)

    rows = [
        ("BTCUSDT", "ema_bounce", 2.4, 1.8),
        ("ETHUSDT", "wick_trap_reversal", 1.2, 0.9),
    ]
    for idx, (symbol, setup_id, pnl_r, pnl_pct) in enumerate(rows, start=1):
        created = report_day.replace(hour=8) + timedelta(minutes=idx)
        await repo.save_signal_outcomes_batch(
            [
                {
                    "tracking_id": f"trk-{idx}",
                    "signal_id": f"sig-{idx}",
                    "tracking_ref": f"REF{idx}",
                    "symbol": symbol,
                    "setup_id": setup_id,
                    "direction": "long",
                    "timeframe": "15m",
                    "created_at": created.isoformat(),
                    "closed_at": (created + timedelta(hours=2)).isoformat(),
                    "result": "tp2_hit",
                    "pnl_pct": pnl_pct,
                    "pnl_r_multiple": pnl_r,
                    "features": {},
                    "was_profitable": True,
                }
            ]
        )

    report = await reporter.generate(date=report_day)

    assert [row["symbol"] for row in report.top_signals] == ["BTCUSDT", "ETHUSDT"]
    assert report.to_dict()["top_signals"][0]["pnl_r_multiple"] == pytest.approx(2.4)
    await repo.close()
