"""Wave I: zero-hit triage and telemetry strategy analysis."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from bot.diagnostics.session_ops import analyze_telemetry, build_zero_hit_triage

if TYPE_CHECKING:
    from pathlib import Path


def test_build_zero_hit_triage_classifies(tmp_path: Path) -> None:
    analysis = tmp_path / "analysis"
    analysis.mkdir()
    (analysis / "strategy_decisions.jsonl").write_text(
        '{"setup_id":"bos_choch","status":"reject","symbol":"BTCUSDT"}\n',
        encoding="utf-8",
    )
    (analysis / "shortlist.jsonl").write_text(
        json.dumps(
            {
                "strategy_fit_counts": {
                    "bos_choch": 3,
                    "fvg_setup": 0,
                }
            }
        )
        + "\n",
        encoding="utf-8",
    )
    live = analyze_telemetry(analysis)
    triage = build_zero_hit_triage(live)
    assert "bos_choch" not in triage["zero_runs"]
    assert triage["strategies_ran"] >= 1
    assert "fvg_setup" in triage["zero_runs"]
