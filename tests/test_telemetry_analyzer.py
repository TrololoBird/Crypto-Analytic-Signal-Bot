from __future__ import annotations

import json
from datetime import datetime, timezone

from scripts.telemetry_analyzer import TelemetryAnalyzer


def _write_jsonl(path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n",
        encoding="utf-8",
    )


def test_telemetry_analyzer_counts_strategy_decision_status_signal(tmp_path) -> None:
    telemetry_dir = tmp_path / "telemetry"
    strategy_file = telemetry_dir / "runs" / "run-1" / "analysis" / "strategy_decisions.jsonl"
    ts = datetime.now(timezone.utc).isoformat()
    _write_jsonl(
        strategy_file,
        [
            {
                "ts": ts,
                "symbol": "BTCUSDT",
                "setup_id": "bos_choch",
                "status": "signal",
                "reason": "pattern.raw_hit",
                "timeframe": "15m",
            },
            {
                "ts": ts,
                "symbol": "BTCUSDT",
                "setup_id": "bos_choch",
                "status": "reject",
                "reason": "pattern.swing_stop_missing_short",
            },
        ],
    )

    analyzer = TelemetryAnalyzer(telemetry_dir, run_id="run-1")

    performance = analyzer.analyze_strategy_performance("bos_choch", hours=1)
    assert performance["signal_count"] == 1
    assert performance["top_reject_reason"] == "pattern.swing_stop_missing_short"
    assert performance["timeframe_distribution"] == {"15m": 1}

    issues = analyzer.detect_calibration_issues()
    assert not any(
        issue.kind == "zero_signal_strategy" and issue.subject == "bos_choch" for issue in issues
    )
    assert not any(
        issue.kind == "priority_asset_zero_signals" and issue.subject == "BTCUSDT"
        for issue in issues
    )
