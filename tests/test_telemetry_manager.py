from __future__ import annotations

from types import SimpleNamespace

from bot.application.telemetry_manager import TelemetryManager
from bot.core.engine import StrategyDecision
from bot.models import Signal


class _CaptureTelemetry:
    def __init__(self) -> None:
        self.rows: list[tuple[str, dict[str, object]]] = []

    def append_jsonl(self, filename: str, row: dict[str, object]) -> None:
        self.rows.append((filename, row))


def test_strategy_decision_signal_telemetry_includes_signal_quality_fields() -> None:
    telemetry = _CaptureTelemetry()
    bot = SimpleNamespace(
        telemetry=telemetry,
        settings=SimpleNamespace(
            runtime=SimpleNamespace(diagnostic_trace_limit_per_symbol=0)
        ),
        _diagnostic_trace_counts={},
    )
    manager = TelemetryManager(bot)
    signal = Signal(
        symbol="ETHUSDT",
        setup_id="spread_strategy",
        direction="short",
        score=0.60182,
        timeframe="15m",
        entry_low=100.0,
        entry_high=100.0,
        stop=102.0,
        take_profit_1=97.0,
        take_profit_2=95.0,
        spread_bps=0.04,
        atr_pct=0.34,
        oi_change_pct=0.12,
        ls_ratio=1.5,
    )

    manager.append_strategy_decision(
        symbol="ETHUSDT",
        trigger="test",
        decision=StrategyDecision.signal_hit(
            setup_id="spread_strategy",
            signal=signal,
        ),
    )

    assert len(telemetry.rows) == 1
    filename, row = telemetry.rows[0]
    assert filename == "strategy_decisions.jsonl"
    assert row["status"] == "signal"
    assert row["timeframe"] == "15m"
    assert row["score"] == 0.6018
    assert row["risk_reward"] == 1.5
    assert row["stop_distance_pct"] == 2.0
    assert row["spread_bps"] == 0.04
    assert row["atr_pct"] == 0.34
    assert row["oi_change_pct"] == 0.12
    assert row["ls_ratio"] == 1.5
