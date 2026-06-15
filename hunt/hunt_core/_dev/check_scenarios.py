"""Scenario must-pass + family-vote checks (§4 / §R.2)."""
from __future__ import annotations

import sys

from hunt_core.confluence.confluence import (
    FAMILY_VOTE_MIN,
    evaluate_must_pass,
    family_vote_count,
)
from hunt_core.confluence.mtf import MTFConfluence, ScenarioScore, TFSignal
from hunt_core.deliver.dispatch import evaluate_delivery


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
    }
    ok, missing = evaluate_must_pass(row, direction="short")
    if not ok:
        print(f"FAIL unexpected block: {missing}", file=sys.stderr)
        return 1
    row2 = {"lifecycle": {"recommended_bias": "long"}, "dump": {"dump_score": 80}}
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
        "timeframes": {"1h": {"trend": "bear", "rsi14": 40, "adx14": 30}},
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

    from hunt_core.scan.predump import evaluate_predump
    from hunt_core.scan.prepump import evaluate_prepump
    from hunt_core.scan.presqueeze import evaluate_presqueeze

    tf = {
        "15m": {"rsi14": 72, "closed_rsi14": 72, "adx14": 28},
        "5m": {"closed_macd_hist": -0.001, "bearish": True},
        "1m": {"macd_hist": -0.0005},
    }
    market = {"taker_ratio": 0.95, "depth_imbalance": -0.12}
    pred_row = {
        "symbol": "TESTUSDT",
        "dump": {"dump_score": 70, "support_break_level": 1.0, "levels_viable": True},
        "lifecycle": {"phase": "distribution", "fall_from_high_pct": 5.0},
    }
    dump_out = evaluate_predump(pred_row, price=0.98, tf=tf, market=market)
    if "phase" not in dump_out:
        print("FAIL predump evaluate missing phase", file=sys.stderr)
        return 1

    long_row = {
        "symbol": "TESTUSDT",
        "long": {"long_score": 55, "resistance_break_level": 1.1, "levels_viable": True},
        "lifecycle": {"phase": "accumulation"},
    }
    long_out = evaluate_prepump(long_row, price=1.05, tf=tf, market=market)
    if "phase" not in long_out:
        print("FAIL prepump evaluate missing phase", file=sys.stderr)
        return 1

    squeeze = evaluate_presqueeze(tf, market)
    if squeeze is not None and "squeeze" not in str(squeeze).lower() and not isinstance(squeeze, dict):
        print(f"FAIL presqueeze unexpected type {type(squeeze)}", file=sys.stderr)
        return 1

    print("scenarios ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
