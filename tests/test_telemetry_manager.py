from __future__ import annotations

from types import SimpleNamespace
from datetime import UTC, datetime

from bot.application.telemetry_manager import TelemetryManager
from bot.core.engine import StrategyDecision
from bot.domain.schemas import PipelineResult, Signal


class _CaptureTelemetry:
    def __init__(self) -> None:
        self.rows: list[tuple[str, dict[str, object]]] = []

    def append_jsonl(self, filename: str, row: dict[str, object]) -> None:
        self.rows.append((filename, row))


def test_strategy_decision_signal_telemetry_includes_signal_quality_fields() -> None:
    telemetry = _CaptureTelemetry()
    bot = SimpleNamespace(
        telemetry=telemetry,
        settings=SimpleNamespace(runtime=SimpleNamespace(diagnostic_trace_limit_per_symbol=0)),
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
    assert row["reason_family"] == "pattern"
    assert row["entry_low"] == 100.0
    assert row["entry_high"] == 100.0
    assert row["stop"] == 102.0
    assert row["take_profit_1"] == 97.0
    assert row["take_profit_2"] == 95.0
    assert row["stop_distance_pct"] == 2.0
    assert row["spread_bps"] == 0.04
    assert row["atr_pct"] == 0.34
    assert row["oi_change_pct"] == 0.12
    assert row["ls_ratio"] == 1.5


def test_cycle_log_separates_selected_attempts_from_sent_delivery() -> None:
    telemetry = _CaptureTelemetry()
    bot = SimpleNamespace(
        telemetry=telemetry,
        _shortlist_source="ws_light",
        _prepare_error_count=0,
        _ws_manager=None,
        _bus=SimpleNamespace(stats=lambda: {"current_depth": 0}),
        client=SimpleNamespace(state_snapshot=lambda: {}),
        last_cycle_summary={},
    )
    manager = TelemetryManager(bot)
    result = PipelineResult(
        symbol="BTCUSDT",
        trigger="intra_candle",
        event_ts=datetime.now(UTC),
        raw_setups=37,
        funnel={"selected": 1, "delivery_status_counts": {"logged": 1}},
    )
    candidate = Signal(
        symbol="BTCUSDT",
        setup_id="ema_bounce",
        direction="long",
        score=0.7,
        timeframe="15m",
        entry_low=100.0,
        entry_high=100.0,
        stop=98.0,
        take_profit_1=103.0,
        take_profit_2=105.0,
    )

    manager.emit_cycle_log(
        symbol="BTCUSDT",
        interval="bookTicker",
        event_ts=datetime.now(UTC),
        shortlist_size=45,
        tracking_events=[],
        result=result,
        candidates=[candidate],
        rejected=[],
        delivered=[],
    )

    cycle_row = next(row for name, row in telemetry.rows if name == "cycles.jsonl")
    symbol_row = next(
        row for name, row in telemetry.rows if name == "symbol_analysis.jsonl"
    )
    assert cycle_row["selected_count"] == 1
    assert cycle_row["selected_signals"] == 1
    assert cycle_row["delivered_count"] == 0
    assert cycle_row["delivered_signals"] == 0
    assert cycle_row["delivery_status_counts"] == {"logged": 1}
    assert symbol_row["selected_signals"] == 1
    assert symbol_row["delivered"] == 0
    assert bot.last_cycle_summary["selected_signals"] == 1
    assert bot.last_cycle_summary["delivered_signals"] == 0
