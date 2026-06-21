"""Universe scan smoke — rank + alert selection on synthetic or cached rows."""
from __future__ import annotations

import argparse
import asyncio
import sys
from typing import Any


def _synthetic_rows() -> list[dict[str, Any]]:
    """Minimal rows for offline rank/alert path (no network)."""
    from hunt_core._dev.check_expansion import _pre_pump_row

    hot = _pre_pump_row(activation_pct=1.0)
    hot["symbol"] = "HOTUSDT"
    warm = _pre_pump_row(activation_pct=8.0)
    warm["symbol"] = "WARMUSDT"
    warm["market"]["oi_z"] = 1.1
    return [hot, warm]


async def _run(*, live: bool) -> int:
    from hunt_core.analysis.expansion_engine.config import load_expansion_config
    from hunt_core.analysis.expansion_engine.ranking.scan import rank_universe
    from hunt_core.runtime.expansion_universe_scan import (
        collect_universe_rows,
        select_universe_alerts,
        write_expansion_scan_jsonl,
    )

    cfg = load_expansion_config()
    if live:
        from hunt_core.bootstrap import bootstrap

        bootstrap()
        rows_map = collect_universe_rows()
        rows = list(rows_map.values())
        source = "live_cache"
    else:
        rows = _synthetic_rows()
        source = "synthetic"

    if not rows:
        print(f"FAIL: no rows ({source})", file=sys.stderr)
        return 1

    ranked = rank_universe(rows, cfg=cfg, top_n=cfg.tg_universe_top_n)
    write_expansion_scan_jsonl(ranked)
    alerts = select_universe_alerts(ranked, cfg)

    pump_n = len(ranked.get("pre_pump") or [])
    dump_n = len(ranked.get("pre_dump") or [])
    alert_p = len(alerts.get("pre_pump") or [])
    alert_d = len(alerts.get("pre_dump") or [])

    print(f"source={source} rows={len(rows)} ranked_pump={pump_n} ranked_dump={dump_n}")
    print(f"alert_candidates pump={alert_p} dump={alert_d}")
    for side in ("pre_pump", "pre_dump"):
        for opp in (ranked.get(side) or [])[:3]:
            print(
                f"  {side} {opp.symbol} opp={opp.meta.opportunity_score:.3f} "
                f"state={opp.state} trig={opp.trigger_probability:.2f}"
            )

    if not live and pump_n < 1:
        print("FAIL: expected at least one pre_pump from synthetic rows", file=sys.stderr)
        return 1
    print("OK: universe scan smoke")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Expansion universe scan smoke")
    parser.add_argument(
        "--live",
        action="store_true",
        help="Use hunt_scan_store + deep_query_store (requires running watch cache)",
    )
    args = parser.parse_args(argv)
    return asyncio.run(_run(live=args.live))


if __name__ == "__main__":
    raise SystemExit(main())
