"""Factor panel self-check — §R.2."""
from __future__ import annotations

import math
import sys

from hunt_core.features.factors import build_factor_panel


def main() -> int:
    row = {
        "timeframes": {
            "15m": {"rsi14": 62.0, "adx14": 22.0},
            "1h": {"adx14": 28.0},
        },
        "market": {"taker_ratio": 1.05, "oi_z": 1.2, "funding_pct": 0.0001},
    }
    panel = build_factor_panel(row)
    bad: list[str] = []
    for key, val in panel.items():
        if val is None:
            bad.append(f"{key}=None")
            continue
        if not math.isfinite(val):
            bad.append(f"{key} not finite")
        elif val < -1.01 or val > 1.01:
            bad.append(f"{key} out of range {val}")
    absent_row = {"timeframes": {}, "market": {}}
    absent_panel = build_factor_panel(absent_row)
    if absent_panel:
        bad.append(f"absent row produced factors: {list(absent_panel)}")
    print(f"factors checked={len(panel)} bad={len(bad)}")
    for b in bad:
        print(f"  FAIL {b}", file=sys.stderr)
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
