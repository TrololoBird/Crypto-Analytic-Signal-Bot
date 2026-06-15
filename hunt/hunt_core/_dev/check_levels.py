"""Level geometry self-check — §R.2 / B1 vol-floor."""
from __future__ import annotations

import sys

from hunt_core.levels.levels import fib_retracement_levels, structural_short_levels


def main() -> int:
    fib = fib_retracement_levels(120.0, 80.0)
    short = structural_short_levels(
        price=100.0,
        impulse_high=120.0,
        impulse_low=80.0,
        fib=fib,
        atr15=2.0,
        atr1h=3.0,
        local_support=95.0,
        local_resistance=110.0,
        symbol="TESTUSDT",
    )
    bad: list[str] = []
    sl = float(short.get("stop_loss") or 0)
    if sl > 0 and (sl - 100.0) < 3.0 * 0.6:
        bad.append(f"short SL too tight vs 1h ATR floor (sl={sl})")
    rr = short.get("risk_reward")
    if rr is not None and float(rr) < 1.0:
        bad.append(f"short R:R {rr} < 1")
    if not short.get("viable", True):
        bad.append("short not viable")
    print(f"levels ok={not bad}")
    for b in bad:
        print(f"  FAIL {b}", file=sys.stderr)
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
