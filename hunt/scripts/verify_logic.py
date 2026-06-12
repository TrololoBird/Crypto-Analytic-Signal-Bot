#!/usr/bin/env python3
"""Full hunt logic verification — synthetic post-mortem cases + optional live symbols."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime

from hunt_watch.bootstrap import bootstrap

bootstrap()

from hunt_watch.logic_verify import (
    run_adx_prep_cases,
    run_btc_corr_cases,
    run_confirm_cases,
    run_delivery_cases,
    run_sniper_cases,
    run_early_cases,
    run_ensemble_cases,
    run_frame_fallback_cases,
    run_lifecycle_cases,
    run_lifecycle_sticky_cases,
    run_mtf_cases,
    run_orderflow_cases,
    run_phase_change_policy_cases,
    run_phase_matrix_cases,
    run_delivery_regime_cases,
    run_dump_continuation_cases,
    run_tracker_be_cases,
    run_tracker_outcome_cases,
    run_stale_grace_cases,
    run_stale_entry_phase_cases,
    run_feature_latch_cases,
    run_fast_flush_tp1_cases,
    run_dump_init_score_cases,
    run_backtest_synthetic_cases,
    run_prep_shadow_cases,
    run_replay_cases,
    run_ws_research_cases,
    summarize,
)


async def _live_audit(symbols: list[str]) -> dict:
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location(
        "critical_audit_mod",
        Path(__file__).resolve().parent / "critical_audit.py",
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    reports = []
    for sym in symbols:
        print(f"live {sym} …", file=sys.stderr, flush=True)
        row = await mod._bot_row(sym)
        if row.get("error"):
            reports.append({"symbol": sym, "ok": False, "issues": [row["error"]]})
        else:
            reports.append(mod._compare(sym, row))
    return {"live": reports}


async def _main_async(*, live: list[str]) -> int:
    syn_lc = run_lifecycle_cases()
    syn_sticky = run_lifecycle_sticky_cases()
    syn_pcp = run_phase_change_policy_cases()
    syn_conf = run_confirm_cases()
    syn_sniper = run_sniper_cases()
    syn_del = run_delivery_cases()
    syn_early = run_early_cases()
    syn_adx = run_adx_prep_cases()
    syn_mtf = run_mtf_cases()
    syn_of = run_orderflow_cases()
    syn_rep = run_replay_cases()
    syn_ps = run_prep_shadow_cases()
    syn_dr = run_delivery_regime_cases()
    syn_dc = run_dump_continuation_cases()
    syn_be = run_tracker_be_cases()
    syn_out = run_tracker_outcome_cases()
    syn_stale = run_stale_grace_cases()
    syn_stale_entry = run_stale_entry_phase_cases()
    syn_latch = run_feature_latch_cases()
    syn_ff_tp = run_fast_flush_tp1_cases()
    syn_di = run_dump_init_score_cases()
    syn_bt = run_backtest_synthetic_cases()
    syn_pm = run_phase_matrix_cases()
    syn_btc = run_btc_corr_cases()
    syn_ff = run_frame_fallback_cases()
    syn_ws = run_ws_research_cases()
    syn_ens = run_ensemble_cases()
    syn = summarize(
        syn_lc
        + syn_sticky
        + syn_pcp
        + syn_conf
        + syn_sniper
        + syn_del
        + syn_early
        + syn_adx
        + syn_mtf
        + syn_of
        + syn_rep
        + syn_ps
        + syn_dr
        + syn_dc
        + syn_be
        + syn_out
        + syn_stale
        + syn_stale_entry
        + syn_latch
        + syn_ff_tp
        + syn_di
        + syn_bt
        + syn_pm
        + syn_btc
        + syn_ff
        + syn_ws
        + syn_ens
    )
    out: dict = {"ts": datetime.now(UTC).isoformat(), "synthetic": syn}
    if live:
        out["live_audit"] = await _live_audit(live)
    print(json.dumps(out, indent=2, default=str))
    syn_bad = syn["failed"] > 0
    live_bad = any(not r.get("ok") for r in out.get("live_audit", {}).get("live", []))
    return 1 if syn_bad or live_bad else 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Hunt logic verification")
    parser.add_argument("--live", nargs="*", default=[], help="Live symbols e.g. BEATUSDT BTCUSDT")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(_main_async(live=[s.upper() for s in args.live])))


if __name__ == "__main__":
    main()
