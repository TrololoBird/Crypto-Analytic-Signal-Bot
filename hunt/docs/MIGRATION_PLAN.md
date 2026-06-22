# MIGRATION_PLAN — Hunt audit remediation (executable checklist)

Date: 2026-06-21 · Operator decisions: **A1** (fusion core), **E1** (lab split), **B1** (pinned=4).

## Phase 0 — Data integrity ✅

- [x] **G1** Closed-bar provenance: `build_feature_vector(require_closed=True)` → `build_live_detection` abstains on forming bar (`detect/live.py`, `features/feature_engine.py`, `runtime/tick_fusion.py`).
- [x] **I1** Ledger geometry on deliver + block rows (`track/outcome_ledger.py` `_setup_geometry`).
- [x] **H1** Fusion TOML live: `fusion_params()` defaults in `build_live_detection` (`detect/live.py`, `config.defaults.toml [fusion]`).
- [x] **Wash upstream** Factor abstain when wash pre-check fails (`gate/_wash.py`, `detect/factors.py`).
- [x] **Scanner drift** `scanner_thresholds()` + `[scanner]` TOML (`params/store.py`, `data/scanner.py`).

## Phase 1 — Quick fixes ✅

- [x] Remove dead `fetch_taker_buy_sell_volume` path (`market/client.py` → fapi only).
- [x] Remove `"3m"` from entry confirm allowed set (`params/store.py`).
- [x] Archetype aliases: `coil_long` → `prepump_long` (`analysis/archetypes.py`, `maps/forecast.py`, `playbook_checks.py`).
- [x] `ev_primary_default = false`, `HUNT_EV_BOOTSTRAP` default `"0"` (`config.defaults.toml`, `setups/catalog.py`).

## Phase 2 — Lab vs production ✅

- [x] `TELEGRAM_LAB_CHAT_ID` / `lab_chat_id()` (`secrets.py`, `delivery/lab.py`).
- [x] `route_delivery_lane` + `send_lane_html` for confirm/armed TG (`deliver/dispatch.py`, `_cycle_tick.py`).
- [x] Separate lab ledger (`delivery/delivery_state.LAB_LEDGER_PATH`, `_cycle_ledger.py`).
- [x] Expansion pinned alerts → lab channel (`runtime/expansion_alerts.py`).

## Phase 2b — Unified cooldown ✅

- [x] Cross-channel key `xchan:{sym}:{dir}` (`delivery/delivery_state.py`).
- [x] Wired into `unified_cooldown_ok` / `mark_unified_sent` (`deliver/dispatch.py`).
- [x] Persisted via `_load_state` / `_save_state` merge (`runtime/cycle/_impl.py`).

## Phase 3 — Single arbiter (A1) ✅

- [x] `evaluate_confirm_authorities` requires fusion + playbook + mission (`delivery/arbiter.py`).
- [x] Production lane in `evaluate_delivery` runs arbiter before TG (`deliver/dispatch.py`).
- [x] Catalog EV promotion lab-only (`setups/catalog.py` `delivery_lane=lab`, `sync_ev_primary_confirm` guard).

## Phase 4 — Prescan (D1) ✅

- [x] `[watch.prescan]` debounce 90s (60–120 clamp), merge_cap 12 (`config.defaults.toml`, `params/store.py`).
- [x] `_cycle_loop.py` reads `prescan_thresholds()`.

## Phase 5 — Debloat (partial) ✅

- [x] `runtime/tick_fusion.py` (fusion wiring).
- [x] `runtime/cycle/_cycle_ledger.py` (ledger append).
- [x] `market/_fapi_ratio.py` (fapi ratio helper scaffold).

## Phase 6 — Calibration ✅

- [x] `python -m hunt_core._dev.replay_ledger_counterfactual` — geometry coverage + horizon stats.

## Verify gates

```bash
python3 -m compileall -q hunt_core
python3 -m hunt_core._dev.check_logic
python3 -m hunt_core._dev.check_factors_fusion
python3 -m hunt_core._dev.replay_fusion --all --q-gate 0.92
python3 -m hunt_core._dev.authority_audit
python3 -m hunt_core._dev.check_ccxt
```

## Operator config migration

1. Set `TELEGRAM_LAB_CHAT_ID` for exploratory alerts (expansion, EV bootstrap).
2. Keep `HUNT_EV_BOOTSTRAP=0` in production.
3. Fusion keys in `[fusion]` now affect live detection (`q_gate`, `q_phase`, `lookback`).
4. Pinned defaults: BTC, ETH, XAU, XAG only — add SOL/XRP/PAXG in `config.toml` if needed.
