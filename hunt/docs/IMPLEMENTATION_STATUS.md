# Hunt Two-Module Rebuild — implementation status

> Canon: **Module 1 = Deep** · **Module 2 = Scanner** · Plan: `abstract-chasing-cerf.md`

## Summary (2026-06-23)

**Logic redesign P0–P9 — complete** (abstract-chasing-cerf scope). Full offline suite green.

**Architectural debt** (config drift, Signal Ledger, authority funnel stats, backtest gap) is tracked separately in [`ARCHITECTURE_DEBT.md`](ARCHITECTURE_DEBT.md) — not part of P0–P9 closure.

## Phase checklist (abstract-chasing-cerf)

| Phase | ✅ | Deliverable |
|-------|---|-------------|
| **P0** | ✅ | `hunt_core/signals/` — model, lifecycle, emit; `setup_id` dedup; tracker + outcome ledger hookup |
| **P1** | ✅ | Structural entry zone; catalyst≠stop; canonical levels; TP envelope move band; `min_rr_tp1=1.0` |
| **P2** | ✅ | CVD by sign; absorption/footprint/iceberg in engines; `map_footprint_delta` |
| **P3** | ✅ | `_prospective_levels` deleted; realized-only heatmap; OI-forward in map builder |
| **P4** | ✅ | Reconcile on honest liq zones; strong_conflict→WAIT |
| **P5** | ✅ | `assess_preparation_readiness`; scanner via `build_scanner_signal` |
| **P6** | ✅ | `SignalEmitter` pinned loop; activation TG block; cold-start startup only |
| **P7** | ✅ | XAU≡PAXG collapse; «Сила сигнала» / «Приоритет очереди» |
| **P8** | ✅ | Cross-venue wall merge; `as_of` pinned path; continuation guard |
| **P9** | ✅ | Legacy paths deleted; fail-closed config keys; docs updated |

## §5 critical items

| # | Item | Status |
|---|------|--------|
| MAJOR-1 | Remove emission quota (`target_signal_rate`) | ✅ |
| MAJOR-2 | Cross-venue DOM merge by price bucket | ✅ |
| 3 | Reuse `track/tracker.py` | ✅ |
| 4 | Silence on WAIT (not gate change) | ✅ |
| 6 | Fail-closed unknown `verdict_v2` keys | ✅ |
| 7 | `as_of` on pinned path | ✅ |
| 10 | Deep outcome ledger on emit | ✅ |
| 11 | Scanner → `/signal SYM` link in delivery card | ✅ |
| 13 | Price sanity withhold | ✅ `shared/price_sanity.py` |

## Removed legacy paths (detection stack)

`deep_change_fingerprint` · `_prospective_levels` · `target_signal_rate` · `auto_tune_*` · `should_send_pinned_batch` · `scan/{predump,prepump,…}` · `regime/leg_fsm`

**Compat stubs remain** (`detect/legacy_compat.py`, `probe_compat.py`, `scan/scanner.py` shim) — see `ARCHITECTURE_DEBT.md`. Wording **“legacy detection removed”** is accurate; **“zero legacy”** is not.

## Legacy marker audit (touched modules)

**0** hits for `legacy|compat|deprecated|TODO|FIXME|HACK` in: `signals/`, `verdict_v2/{levels,catalyst,calibration,config}.py`, `maps/liquidation.py`, `scanner/gate/_mission.py`.

## Verification (2026-06-22 — all green)

```bash
hunt/.venv/bin/python -m compileall -q hunt/hunt_core
cd hunt && .venv/bin/python -m hunt_core._dev.check_imports
.venv/bin/python -m hunt_core._dev.check_verdict_v2
.venv/bin/python -m hunt_core._dev.check_deep
.venv/bin/python -m hunt_core._dev.check_logic
.venv/bin/python -m hunt_core._dev.replay_fusion
.venv/bin/python -m hunt_core._dev.budget
```

**Render harness (§0):** ETHUSDT live `assemble_deep_tick` → `format_deep_from_row` — OK (24 lines, `as_of` stamped, catalyst≠stop visible).

**Docs:** root `CLAUDE.md` Hunter row expanded; `.claude/rules/hunt-logic-redesign.md` added.

## Git (§0 plan — committed 2026-06-22)

`redesign-p0` … `redesign-p9` on branch **`fix/hunt-phase0-data-integrity`** (fast-forward from `dev`, 2026-06-22). Suite green.

## Out of scope (operator)
| Supervised live `watch` soak | ✅ smoke `watch --once` exit 0; multi-hour soak — operator |
