import json
from pathlib import Path

from scripts.analyze_session import analyze_run


def _write_jsonl(path: Path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")


def test_analyze_run(tmp_path: Path):
    run_dir = tmp_path / "run1"
    raw = run_dir / "raw"
    analysis = run_dir / "analysis"

    signals = [
        {"setup_id": "s1", "tracking_ref": "t1"},
        {"setup_id": "s1", "tracking_ref": "t2"},
        {"setup_id": "s2", "tracking_ref": "t3"},
    ]
    outcomes = [
        {"tracking_ref": "t1", "result": "stop_loss", "pnl_r_multiple": -1},
        {"tracking_ref": "t2", "result": "take_profit", "pnl_r_multiple": 2},
    ]

    _write_jsonl(raw / "signals.jsonl", signals)
    _write_jsonl(analysis / "outcomes.jsonl", outcomes)

    out_json, out_md = analyze_run(run_dir)
    assert Path(out_json).exists()
    assert Path(out_md).exists()

    data = json.loads(Path(out_json).read_text(encoding="utf-8"))
    assert data["total_signals"] == 3
    assert data["total_outcomes"] == 2
    assert "s1" in data["per_setup"]
