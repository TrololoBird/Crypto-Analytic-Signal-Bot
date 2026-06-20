"""Scenario must-pass + family-vote checks (§4 / §R.2)."""
from __future__ import annotations

import sys

from hunt_core.confluence.confluence import (
    FAMILY_VOTE_MIN,
    evaluate_must_pass,
    family_vote_count,
)
from hunt_core.confluence.mtf import MTFConfluence, ScenarioScore
from hunt_core.deliver.dispatch import evaluate_delivery


def _closed_tf(*, close: float = 1.0) -> dict[str, dict[str, object]]:
    bar = {"closed_bar": True, "close": close}
    return {"5m_closed": bar, "15m_closed": bar}


def _low_vote_mtf() -> MTFConfluence:
    neutral = ScenarioScore(
        direction="long",
        score=0.4,
        htf_count=0,
        htf_total=3,
        entry_lo=0,
        entry_hi=0,
        tp1=0,
        tp2=0,
        stop=0,
    )
    short_sc = ScenarioScore(
        direction="short",
        score=0.5,
        htf_count=0,
        htf_total=3,
        entry_lo=0,
        entry_hi=0,
        tp1=0,
        tp2=0,
        stop=0,
    )
    return MTFConfluence(
        symbol="TESTUSDT",
        price=1.0,
        tf_signals={},
        long_scenario=neutral,
        short_scenario=short_sc,
        dominant="neutral",
    )


def main() -> int:
    row = {
        "lifecycle": {"recommended_bias": "short"},
        "dump": {"dump_score": 70, "confirmed": True},
        "long": {"long_score": 30},
        "timeframes": _closed_tf(),
    }
    ok, missing = evaluate_must_pass(row, direction="short")
    if not ok:
        print(f"FAIL unexpected block: {missing}", file=sys.stderr)
        return 1
    row2 = {
        "lifecycle": {"recommended_bias": "long"},
        "dump": {"dump_score": 80},
        "timeframes": _closed_tf(),
    }
    ok2, miss2 = evaluate_must_pass(row2, direction="short")
    if ok2 or "htf_bias_veto" not in miss2:
        print(f"FAIL expected htf veto got ok={ok2} miss={miss2}", file=sys.stderr)
        return 1

    mtf = _low_vote_mtf()
    votes = family_vote_count(mtf, direction="short")
    if votes >= FAMILY_VOTE_MIN:
        print(f"FAIL expected low family vote got {votes}", file=sys.stderr)
        return 1
    low_vote_row = {
        "symbol": "TESTUSDT",
        "price": 1.0,
        "lifecycle": {"recommended_bias": "short", "phase": "dump_active"},
        "timeframes": {**_closed_tf(), "1h": {"trend": "bear", "rsi14": 40, "adx14": 30}},
        "mtf": mtf,
        "dump": {
            "confirmed": True,
            "dump_score": 75,
            "entry_zone": [1.01, 1.02],
            "stop_loss": 1.05,
            "tp1": 0.95,
            "tp2": 0.90,
            "risk_reward": 2.0,
            "levels_viable": True,
        },
        "long": {"long_score": 20},
    }
    gate, tier = evaluate_delivery(
        low_vote_row,
        direction="short",
        setup=low_vote_row["dump"],
        lifecycle=low_vote_row["lifecycle"],
        symbol="TESTUSDT",
    )
    if gate.ok or "family_vote" not in str(gate.code):
        print(f"FAIL expected family_vote block got ok={gate.ok} code={gate.code}", file=sys.stderr)
        return 1

    # Legacy predump/prepump/presqueeze evaluators removed — fusion owns detection.
    print("SKIP legacy scan scenario evaluators (fusion cutover)")

    high_vote = MTFConfluence(
        symbol="TESTUSDT",
        price=1.0,
        tf_signals={},
        long_scenario=ScenarioScore(
            direction="long", score=0.3, htf_count=0, htf_total=3,
            entry_lo=0, entry_hi=0, tp1=0, tp2=0, stop=0,
        ),
        short_scenario=ScenarioScore(
            direction="short", score=0.9, htf_count=3, htf_total=3,
            entry_lo=0.99, entry_hi=1.0, tp1=0.9, tp2=0.85, stop=1.05,
        ),
        dominant="short",
    )
    if family_vote_count(high_vote, direction="short") < FAMILY_VOTE_MIN:
        print("FAIL expected high family vote fixture", file=sys.stderr)
        return 1

    from hunt_core.deliver.dispatch import evaluate_forming_gate

    forming_gate = evaluate_forming_gate(
        {"confirmed": False, "dump_score": 50},
        direction="short",
        symbol="TESTUSDT",
    )
    if not forming_gate.ok and forming_gate.code == "invalid_setup":
        print(f"FAIL forming gate unexpected block {forming_gate.code}", file=sys.stderr)
        return 1

    print("scenarios ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
