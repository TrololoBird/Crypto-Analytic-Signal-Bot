"""Scenario must-pass checks (§4 / §R.2)."""
from __future__ import annotations

import sys

from hunt_core.confluence.confluence import evaluate_must_pass


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
    print("scenarios ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
