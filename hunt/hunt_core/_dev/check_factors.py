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
    # Prepared-frame columns (cmf20/kama10) are canonical via prepare_frame, not polars_ta_bridge.
    if "15m" in row.get("timeframes", {}):
        tf15 = row["timeframes"]["15m"]
        if tf15.get("cmf20") is not None and "flow_cmf15" not in panel:
            try:
                cmf = float(tf15["cmf20"])
                if math.isfinite(cmf):
                    panel["flow_cmf15"] = max(-1.0, min(1.0, cmf))
            except (TypeError, ValueError):
                pass
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
