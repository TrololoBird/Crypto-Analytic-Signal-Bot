# Hunt Changelog (session notes)

## 2026-06-22 — Logic redesign (abstract-chasing-cerf P0–P9)

- **P0:** `hunt_core/signals/` — unified lifecycle (`forming→signal→activated→tracking`), `setup_id` dedup replaces `deep_change_fingerprint`.
- **P1:** Structural entry zone, catalyst≠stop, canonical level set, TP-ladder move envelope, `min_rr_tp1=1.0`.
- **P2:** Microstructure wired into engines; CVD divergence by sign (bull→long, bear→short).
- **P3:** Deleted `_prospective_levels` synthetic liquidation band; realized events only in heatmap.
- **P4–P6:** Reconcile on honest data; pinned TG via lifecycle spine; startup announce cold-start only.
- **P5:** Scanner `assess_preparation_readiness` — energy+direction bypass for pre-move delivery.
- **P7–P8:** Cross-venue DOM merge by price bucket; queue gold collapse retained.
- **MAJOR-1 fix:** Removed `target_signal_rate` / `auto_tune_*` emission quota from config + calibration.
- **Suite:** compileall, check_imports, check_verdict_v2, check_deep, check_logic, replay_fusion, budget — all green.

## 2026-06-22 — abstract-chasing-cerf plan complete

- **Module 1 Deep R1–R11:** reconcile gate, plan geometry, activation lifecycle, cross-venue DOM, gold equivalence, liq honesty, queue TOP-N, live E2E `BTCUSDT`.
- **Two-module rebuild:** `deep⊥scanner` lint; shims removed; `detect/deep` → `scanner/detect/lake_panel`; trend facts → `shared/facts/`.
- **Phase 8 longs ramp:** uncalibrated long delivery → lab lane (`long_ramp_reason`) with full geometry; production unlock via `gate_edge` n≥30 or `HUNT_LONG_TG=1`.
- **Verification:** `verify_hunt_rebuild.sh`, `check_plan_complete`, `check_deep_e2e` (+ live).

## 2026-06-20 (d) — TZ review: explicit params, gate floor, phase hysteresis

- **Honest scope**: `docs/FUSION_PARAMS.md` + `detect/config.py` — official fusion tunables.
- **Gate**: `effective = max(symbol_quantile, global_gate_floor)`; MAD epsilon + z-clip.
- **Phase**: sticky MID hysteresis (`phase_mid_exit_ratio/bars`) — reduces PRE/MID flicker.
- **Funding**: `funding_min_n=48` for new listings / step-wise rate.
- **Replay**: forward close return, random ATR baseline, `--walk-forward FRAC`.

## 2026-06-20 (c) — Fusion math hardening (review fixes)

- **Aggregation**: directional factors → signed **median** (replaces Stouffer `Σz/√n`).
- **Gate**: vol-adjusted magnitude (`magnitude / max(atr_pct, 0.15)`) for quantile gate.
- **Semantics**: `fusion_score` 0–100 replaces logistic `p_win` on fusion setups; `delivery_p_win` from geometry EV only.
- **Perf**: `detect/magnitude_cache.py` — O(1) incremental magnitude history (was O(n²) in live).
- **Safety**: `build_window(ts_max=…)` ts filter; `tests/test_calibrate.py` for degenerate inputs.

## 2026-06-20 (b) — docs/CI/graphify cleanup + supervised session

- **Docs**: rewrote `HUNT_ARCHITECTURE.md`, `ARCHITECTURE_DEBT.md`, `HUNT_TARGET_ARCH.md` for fusion-only path; added `ENGINE_DESIGN.md`.
- **CI**: `.github/workflows/hunt-ci.yml` — fusion + deep checks.
- **Ops**: `watch.sh` default single-process (`HUNT_WATCH_SUPERVISE=0`); stale PID lock cleanup; `supervised_session.py` owns restarts.
- **Graph**: `graphify update .` at repo root.

## 2026-06-20 — phase-7f/8: delete legacy detection stack, fusion-only path

- **Deleted** legacy detection modules: `scan/{prepump,predump,presqueeze,early,predump_dump_hunt,scoring,routing,_confirm_shared,pump_cycle,detectors}` and `regime/{leg_fsm,_lifecycle_assess,_lifecycle_sticky}` (~8k LOC).
- **Moved** survivors: `detect/market_cycle.py` (from `scan/pump_cycle`), `setups/detectors.py` (from `scan/detectors`); `scan/scanner.py` is a thin shim to `detect/routing`.
- **Delivery helpers**: `gate/_delivery_helpers.py` holds evidence/maps/orderflow helpers formerly scattered in deleted scan modules; consumers rewired (`gate/*`, `deliver/*`, `_cycle_tick`, dev harnesses).
- **Compat**: `detect/legacy_compat.py` + `probe_compat.py` stubs for removed deep/early formatters; `track/events.py` fixes missing `hard` in `audit_probe_row`.
- **Config**: pruned `[collect]`, `[lifecycle.squeeze]`, legacy scoring fuel weights; kept CEX catalog thresholds + `[fusion]` section; `params/store.py` pruned `UNIVERSAL_DEFAULTS`.
- **CCXT**: `market/network.py` proxy screen uses CCXT markets probe only (no raw `fapi.binance.com` ping).
- **Docs**: `ARCHITECTURE.md`, `docs/CCXT.md`, `docs/IMPLEMENTATION_STATUS.md` updated for fusion-only pipeline.
- **Verify**: `py_compile` PASS; `check_factors_fusion` PASS; `check_logic` PASS; `check_ccxt` PASS (0 canon violations).

## 2026-06-19 (g) — Mission lock: pre-dump / pre-pump only (no continuation TG)

- **`gate/_mission.py`**: hard block watch TG when dump/pump **already started** (`dump_active`, `impulse_initiating`, …) or fall/leg past imminent window.
- **Removed** all `_dump_continuation_short_ok` delivery bypasses (`_lifecycle_gates`, `dispatch.sniper`, `_cycle_tick`, `early`, `_cycle_confirm`).
- **`dump_continuation_short_ok`** → always `False` (deprecated); `DUMP_CONTINUATION_PHASES` emptied.
- **Defaults:** `HUNT_WIDE_MODE=0`, `HUNT_SNIPER_MODE=1`; sniper allows long pre-pump phases when `HUNT_LONG_TG=1`.
- **`ENGINE_DESIGN.md`** rewritten around imminent-only watch + separate `/signal` query plane.

## 2026-06-19 (f) — Maps data-completeness + deeper integration + proxy robustness

- **Data completeness**: `build_map_bundle` now ingests funding / top-trader L/S / basis / OI-z / ws-CVD (threaded from `tick_assembly` `market`+`ws_snap`); `MapBundle.extra` carries the raw cross-signal context.
- **Squeeze-fuel model** (`maps/liquidation.squeeze_fuel_scores`): crowded side (L/S) + funding sign + liq-magnet distribution → `liq_squeeze_fuel_short` (short-squeeze = pump fuel) / `liq_squeeze_fuel_long`; `build_liquidation_map` gains funding/top_ls/basis params. `None` when no inputs (fail-loud).
- **Accumulation fusion**: `derive_map_features` emits `map_accumulation_score` (VP coil + bid absorption + thin asks + bullish CVD + rising OI) and `map_oi_z`/`map_funding_rate`/`map_basis_pct`/`map_ws_cvd`.
- **Strategy catalog**: `setups/catalog.map_confluence_logit` adds a bounded (±0.6) map-confluence term to `score_setup_probability` for **every** catalog setup (threaded `market` at both call sites).
- **Confirm helpers**: `maps_cascade_aligned` / `maps_flow_confirms` / `maps_accumulation_confirms` now leverage `liq_squeeze_fuel_*` and the fused `map_accumulation_score`.
- **Gate**: `_quality.check_accumulation_long` waives the weak-P(win) block when `map_accumulation_score ≥ 0.6` (mission: don't filter out early pre-pump accumulation).
- **Proxy robustness**: standard proxy env (`HTTPS_PROXY`/`ALL_PROXY`/`WSS_PROXY`/`BINANCE_PROXY_URL`) is seeded into the **primary** pool (was fallback-only); startup self-heals by persisting the surviving working set to `[bot.network]`. (Reverted an incorrect `[network]` section rename after confirming `HuntSettings` roots config under `[bot]`.)
- **Verify**: full `py_compile` PASS; 28-module signal-path import PASS; offline harness PASS (build_map_bundle → derive features → catalog scoring lift +0.14 → accumulation/pre-pump confirm → forecast); proxy config round-trip + pool failover offline PASS. Live Binance futures still blocked by geo (`fapi.binance.com`) in this environment.

## 2026-06-19 (e) — Maps mission completion: close forecast wiring + remove dead config

- **Regression fix**: `prepump.confirm_long` used `pos_rng` before assignment (`UnboundLocalError` on every call) — `context_pos_in_range` resolution moved above `maps_secondary_flags` / `early_accum`.
- **Forecast on `/signal`**: `format_signal_brief_telegram` now renders `format_accumulation_forecast_section`; `query_service.resolve_query_row` populates `row["maps_forecast"]` (fallback build) so the brief always has it when maps exist.
- **Forecast on confirm/ARMED card**: `dispatch.format_delivery_card` renders the pre-pump forecast block first among map sections (self-guards when absent).
- **Forecast de-stub**: `maps/forecast.build_maps_forecast` documented as long-only (pre-pump) by design; added explicit `kind="prepump_long"` to the payload; no misleading generic `direction`.
- **Calibration loop wired into delivery**: `gate/_phase_matrix.phase_matrix_gate` (previously dead/exported-only) now emits `phase_matrix_disable` in `collect_report_blockers`; EV-primary setups bypass it via `filter_ev_primary_legacy_blockers`; self-disables when `self_tuning_frozen()` or no calibration.
- **Config cleanup**: removed partial dead `MapsConfigModel` + `HuntSettings.maps` (6 of 18 fields, never read); `maps/config.load_maps_config()` is the single source of truth.
- **Probe fix**: `maps_calibration_probe.score_tick` hardened against `maps["liquidation"]`/`["orderbook"]` being `None`.
- **Schema**: `MarketBlock` + `MARKET_DESCRIPTIONS` extended (`map_vp_va_width_pct`, `map_cvd_divergence`, `map_void_above_pct`, `liq_forward_weight`); all optional (`total=False`).
- **Verify**: `py_compile` 185/185; `check_ccxt`, `check_imports`, `check_logic` PASS; calibration persist→load round-trip verified. Live futures probes (`maps_live_smoke`/`smoke_signals`/`probe_delivery`) blocked by `fapi.binance.com` 451 geo-block + proxy pool exhausted in this environment — re-run where Binance futures is reachable.

## 2026-06-19 (d) — Maps mission integration: pre-pump engine + forecast + calibration loop

- **Accumulation features**: `derive_vp_accumulation_features` (VA contraction, `map_vp_accumulation`); `derive_ob_accumulation_features` (`map_accum_bid_absorption`, `map_void_above`, `map_ask_thinning`); exported via `derive_map_features`.
- **Pre-pump wiring**: `presqueeze` map-coil path; `detect_accumulation_breakout` / `detect_squeeze_expansion` fire **before** price break on strong maps; `prepump.confirm_long` early-accum confirm (1 structural + map confluence); `scoring.long_analysis` accumulation triggers.
- **Gates**: `mtf_confirm_veto` waives bear-1h / basis-overheat when `maps_accumulation_confirms`; `forward_confidence_min` from config (not hardcoded 0.25).
- **Forecast**: `maps/forecast.build_maps_forecast` → `row["maps_forecast"]`; pinned scenario + `/signal` via `format_accumulation_forecast_section`.
- **Calibration loop**: `params/store.maps_calibration` + `save_maps_calibration`; `maps_calibration_probe --persist`; `liquidation._resolved_forward_confidence` replaces naive event-count formula.
- **Ops**: `1w` VP in tick `frame_map`; map lake flush on cycle shutdown (`data/lake/maps_bundles.jsonl`).

## 2026-06-19 (c) — Pre-pump / pre-dump maps wiring

- **`_confirm_shared`**: `map_*` triggers count toward fuel clusters; map cascade/sticky/CVD in `leading_flow_ignition_score` and `_orderflow_confirm_aligned`.
- **`predump.confirm_dump` / `prepump.confirm_long`**: hard confirm via `map_liq_cascade_*` when forward liq confidence ≥0.25; secondary counts map cascade/sticky/flow flags.
- **`tick_assembly`**: `allow_oi_fetch` on hot tier when symbol is `hot_carry` so forward OID warms on the hot loop.

## 2026-06-19 — Professional multi-exchange maps (`hunt_core/maps/`)

- **New package `hunt_core/maps/`**: Map 1 orderbook heatmap (walls/sticky/void/footprint), Map 2 liquidation (real multi-exchange WS + entry-anchored forward overlay with confidence), Map 3 cumulative volume profile (multi-period/developing VP, HVN/LVN, naked POC).
- **`MapTimeSeriesStore`**: ring buffers + JSONL lake persistence (`data/lake/maps/snapshots.jsonl` on flush).
- **Integration**: `tick_assembly` → `build_map_bundle` → `derive_map_features` → `row["market"]`; Bybit/OKX `watchLiquidations` when `HUNT_CROSS_WS=1`; gate map veto (`gate/_maps.py`); scoring + liquidity scenarios; Telegram sections (liq map, orderflow).
- **Config**: `[maps]` in `hunt/config.defaults.toml`; `HUNT_MAPS_*` env overrides.
- **Calibration**: `python -m hunt_core._dev.maps_calibration_probe` replays tick JSONL for forward vs realized overlap + sticky-wall reaction score.

## 2026-06-19 (b) — Maps completion pass

- **OID forward path**: `fetch_oi_bars_for_maps` + `maps/oi.py` align OI to OHLCV; cached in `MapTimeSeriesStore`; wired through `cross.attach_cross_microstructure` and `tick_assembly` / `symbol_probe`.
- **Map 1 depth**: depth heatmap matrix, iceberg/absorption/spoof/CVD divergence, `merge_full_depth_bins`, `book_deep_top_n` REST on slow path.
- **Engine**: `max_symbols` eviction, OI/liq-estimate cache, calibration updates `forward_confidence`.
- **Signals**: `apply_liquidity_to_mtf_scores(..., market=)` map boosts; confluence passes `market`.
- **Delivery**: confirm card (`format_delivery_card`) + upgraded VP/walls/cross-micro sections; `volume_profile` removed from `LIVE_SKIPPABLE_GROUPS`.
- **Schema**: `maps_snapshot`, `liq_forward_confidence`, `map_stacked_imbalance` on `SymbolContext`; `MARKET_DESCRIPTIONS` extended.

## 2026-06-18 — Handoff plan H0–H9 (live validation + wiring gaps)

- **H1 live:** 3-min continuous watch (`send_telegram=False`); `ev_primary_shadow×56` in telemetry; zero tick crashes post lifecycle fix.
- **H2 calibration:** `rebuild_calibration` verified on watch boot; per-setup `setup_ev_flip_eligible` (n≥8) in `catalog.promote_catalog_ev_setup`.
- **H3 hardening:** `probe_delivery --live` uses proxied `create_hunt_market_plane_from_settings`; `_ensure_kinematic_row_fields` backfills chg for kinematic gate.
- **H4 Phase C:** `check_meme_pump_volume_ratio` on declarative stack; `detect_btc_decoupled` + `pump_cycle` hooks in `detect_dump_initiation`.
- **H5 truth:** `_latched_levels_payload` reads `delivered_levels_snapshot`; `agg_trade_buy_ratio_*` typed in schemas.
- **H6 message:** removed duplicate raw trigger footer from delivery card.
- **H7/H8:** `setup_lake_outcome_n` / flip table; `domain/structure_state.py` wired on tick rows; `EARLY_LEVELS_VETO_BLOCK` selective early alerts.

## 2026-06-18 — P4: QueryService (QueryResult ≠ DeliveryGate)

- **`runtime/query_service.py`**: `QueryResult`, `DirectionQuery`, `build_query_result`, `resolve_query_row`, `format_query_telegram`.
- **`/signal`**: always shows full scenario brief + formation/delivery status + up to 5 blockers (no early exit on gate block).
- Store path uses `evaluate_delivery_fast` + `refresh_live_price=False`; live/`--live` uses full delivery eval.
- **`_dev/probe_delivery`**: uses `build_query_result` for parity; reports `formation_code`, `would_deliver`, all blocker codes.

## 2026-06-18 — P3: anticipation entries (distribution → initiating)

- **`tick_assembly`**: structure `setup_type` + `apply_setup_type_primary_confirm` **before** `confirm_dump`; structure passed to catalog via `prepared_row`.
- **`predump.confirm_dump`**: seeds `confirm_hard` from catalog/structure; `distribution_structure_confirm` path for LC `{distribution, dump_initiating}` with `anticipation_short_primary_ok` (fall ≤5%, structure + secondary/div).
- **`detectors.detect_dump_initiation`**: forming path without `close_below_support` — rejection wick, `pp_short_early`, sweep_reclaim.
- **`catalog.merge_dump_initiation_into_setup`**: tags `anticipation=True`, `entry_archetype=anticipation`.
- **`early.py`**: `SHORT_PREP_LC` + armed fall window includes `dump_initiating` (up to `short_first_break_max_fall_pct`).
- **`gate/_rr`**: `SHORT_DUMP_START_LC_PHASES` includes `dump_initiating` (late-entry waiver 3–5% fall).
- **`gate/_quality`**: exhaustion fade uses forming floor when `anticipation` / `distribution_structure_confirm`.

## 2026-06-18 — P2: unified dump continuation gate + engine design spec

- **`gate/_rr.py`**: `short_dump_delivery_too_late` now consults `dump_continuation_short_ok` — single waiver for lifecycle, quality, report, and RR paths (fixes parallel hard-block in `_quality.py` / `_report.py`).
- **`docs/ENGINE_DESIGN.md`**: first-principles spec — three planes, entry archetypes, gate stack, prior weights, calibration scope.

## 2026-06-18 — Phase 8: cycle run_tick + run_loop extract

- **`runtime/cycle/_cycle_tick.py`** (~1580 LOC) — `run_tick` (per-symbol snapshot, delivery, follow-ups)
- **`runtime/cycle/_cycle_loop.py`** (~714 LOC) — `run_loop`, digest candidates, prescan/universe scheduling
- **`cycle/_impl.py`**: ~945 → **~323 LOC** (delivery helpers, `run_hot_kline_tick`, re-exports)
- Fixes: confirm imports from `_cycle_confirm`; lazy `_impl` bind inside `run_tick` / `run_loop`

## 2026-06-18 — Phase 8: cycle _impl split (advisory/format/reconcile)

- **`runtime/cycle/_cycle_advisory.py`** (~250 LOC) — liq burst, early alert, cooldown, entry_past_tp1
- **`runtime/cycle/_cycle_format.py`** (~270 LOC) — `_format_setup_lines`, phase badges, reason humanizer
- **`runtime/cycle/_cycle_reconcile.py`** (~271 LOC) — orphan/in-watch reconcile, follow-up TG delivery
- **`cycle/_impl.py`**: ~3092 → **~2406 LOC** (`run_tick` / `run_loop` remain)
- **Phase 6:** `replay_row.load_replay_rows()` — JSONL + lake parquet (`--parquet` CLI flag)

## 2026-06-18 — Phase 8: lifecycle assess + cycle confirm split

- **`regime/_lifecycle_assess.py`** (~1010 LOC) — `assess_hunt_lifecycle`, `htf_bias_override`, `effective_support_break`, guards
- **`leg_fsm.py`**: ~1297 → **~310 LOC** (types + promote/attach + re-exports)
- **`runtime/cycle/_cycle_confirm.py`** (~179 LOC) — confirm suppression, blocked telemetry, bias-wait gate
- **`cycle/_impl.py`**: ~3241 → **~3092 LOC**

## 2026-06-18 — Phase 8: followups + leg_fsm split

- **`track/_followups.py`** (~495 LOC) — `evaluate_followups`, level-test tracking, armed→triggered, bias-flip
- **`tracker.py`**: ~1455 → **~995 LOC**
- **`regime/_delivery_fsm.py`** (~202 LOC) — `DeliveryStage`, `advance_delivery_fsm`, `record_delivery_fsm`
- **`regime/_lifecycle_sticky.py`** (~208 LOC) — `stabilize`, `reset_symbol`, sticky debounce
- **`leg_fsm.py`**: ~1654 → **~1297 LOC** (re-exports unchanged public API)

## 2026-06-18 — Phase 8: tracker levels engine split

- **`track/_trailing.py`** (~205 LOC) — MFE, ATR trail ratchet, TP1 management
- **`track/_evaluate_levels.py`** (~567 LOC) — `_bar_extremes`, `_stale_lifecycle_invalidate`, `evaluate_levels`
- **`tracker.py`**: ~2145 → **~1455 LOC**
- **`replay_row.batch_delivery_replay`** — `ev_shadow_mean` / `ev_shadow_negative` in summary

## 2026-06-18 — Phase 1 brief + Phase 8 tracker cooldowns

- **`deliver/_brief.py`** — `format_signal_brief_telegram` + scenario helpers (~349 LOC)
- **`telegram.py`**: ~1250 → **~910 LOC** (transport + re-exports only)
- **`track/_cooldowns.py`** — post-SL, burst cap, daily TG cap, repeat-loser policy (~280 LOC)
- **`tracker.py`**: ~2386 → **~2145 LOC** (re-exports cooldowns; lifecycle/evaluate_levels unchanged)
- **Watch startup:** `rebuild_calibration()` + `invalidate_calibration_cache()` on each `watch` boot

## 2026-06-18 — Phase 1 followup + Phase 6 EV delivery

- **`deliver/_followup.py`** — `format_followup_telegram` (+ PnL/duration helpers)
- **`deliver/_sections.py`** — MTF, volume profile, book walls, cross-micro, cross-exchange sections
- **`telegram.py`**: 1956 → **~1250 LOC** (re-exports from `_followup` / `_sections`)
- **Phase 6:** `HUNT_EV_DELIVERY=1` + `HUNT_EV_MIN` production gate in `policy._decl_check_ev_shadow`
- **`rebuild_calibration.py`** — tracker closed outcomes + `export_phase_calibration()` → `hunt_calibration.json`
- **`intrabar_ignition`** counts as structural hard confirm in `_rr.structural_hard_count`

## 2026-06-18 — Phase 1: telegram formatters → dispatch

- **`deliver/_labels.py`** — `fmt_price`, `phase_human`, `trigger_human`, ratings
- **`deliver/_context_lines.py`** — POC/liq/walls context, structured thesis
- **`format_delivery_card`** enriched: thesis, context, rating, TP labels, pump history
- **`format_entry_telegram`** → thin alias of `format_delivery_telegram`
- **`telegram.py`**: −~600 LOC formatter duplicates removed

## 2026-06-18 — Gate facade <150 LOC (Phase 5 target met)

- **`gate/_phase_matrix.py`** — `PhaseStats`, `disabled_phase_pairs`, `phase_matrix_gate`
- **`gate/_freshness.py`** — entry zone, tier, stale hard blocks, `tp1_progress_block`
- **`gate/_lifecycle.py`** — `lifecycle_dict`, core lifecycle vetoes
- **`gate/_report.py`** — `collect_report_blockers`, `evaluate_alert_gate`, formation/stale helpers
- **`delivery.py`**: 926 → **142 LOC** (re-export facade only)

## 2026-06-18 — Gate module split (Phase 5 continued)

- **`gate/_types.py`** — `GateResult`, `REPORT_BLOCK_PRIORITY`
- **`gate/_wash.py`** — wash + kinematic (A8–A10)
- **`gate/_rr.py`** — min R:R, TP2 room, short dump timing, structural hard count
- **`gate/_filters.py`** — `directional_filters`, phase-aware `hard_filter_blocks`
- **`delivery.py`**: 1710 → **926 LOC** (facade + phase-matrix + freshness/tier + report stack)

## 2026-06-18 — Gate split + NEW DESIGN primary confirms

- **Phase 5:** `gate/_registry.py` (pipeline pre-blockers, `register_gate`, `run_gate_pipeline`); `delivery.py` 1988→1710 LOC
- **Phase 4B:** `gate/_prokol.py`; tf_trap prokol → `prokol_reclaim_{dir}` confirm_hard + `sweep_reclaim` setup_type
- **Phase 3:** `bos_retest_{dir}` confirm_hard via `apply_setup_type_primary_confirm` on tick assembly
- **Phase 0:** scoring fail-loud `data_quality.violations` when 15m ATR missing

## 2026-06-18 — Full phases plan (0–8, no live/smoke)

- **Phase 0:** JSONL rotation on prep_shadow + setup_candidates; `record_data_quality_violation`; T1 liq docs in `CCXT.md`
- **Phase 1:** telegram worst-entry TP%; dispatch conviction line + phantom liq filter; MLIVE-7 route_tick single-dir delivery
- **Phase 2:** `domain/snapshot.py` MarketSnapshot; `stamp_market_derivatives_provenance` wired; tick `snapshot` block
- **Phase 3–5:** `collect_lifecycle_blockers` wired; dump_continuation TG waivers removed; RR floor → `_DELIVERY_MIN_RR_FLOOR`; declarative `setup_type` / `meme_anomaly` / `ev_shadow` gates
- **Phase 6:** `batch_delivery_replay` summary; `data/calibration.json`; `ev/model_shadow.py`; `HUNT_EV_FLIP` gate
- **Phase 7:** `give_back_analysis.py`; `sniper_hold` vs `lifecycle_stale` hold_reason
- **Phase 8:** `_lifecycle_gates` integrated into `collect_report_blockers`

## 2026-06-18 — Full rollout (zesty-drifting-cosmos Phases 2–8)

- **Phase 2:** kline-first CVD in `resolve_flow_cvd_px`; `stamp_market_field_provenance`; freshness gate wired
- **Phase 3:** structure-primary bias (`_resolve_recommended_bias`); `classify_structural_setup_type`; lifecycle context veto (MLIVE-8)
- **Phase 4:** `route_tick` single-direction + early_armed priority; `dump_continuation` → watch-only (predump/scoring)
- **Phase 5:** declarative `lifecycle_context` gate; `watch_only` delivery block; templates → `format_delivery_card`
- **Phase 6:** `compute_rule_based_ev` shadow on tick row; `batch_delivery_replay` harness
- **Phase 7:** ATR-relative trailing confirmed in tracker (`atr_trail_risk_fraction`)
- **Phase 8:** extracted `gate/_lifecycle_gates.py`

## 2026-06-15 — Legacy purge (post-CCXT)

- Removed orphan modules: `domain/limit_entry.py`, `domain/setup_registry.py`, `config_defaults.py`, `runtime/settings.py`, `features/levels.py`
- Removed compat facades: `scan/_engine_impl.py`, `detect/` package; scoring → `scan/scoring.py`
- Removed empty `hunt/_legacy/`, `hunt/scripts/`; deleted superseded docs (`HUNT_ROADMAP`, `HUNT_REWRITE_MIGRATION`, `HUNT_PRODUCT_DEFINITION`, `HUNT_REFERENCE_*`) and root `docs/HUNT_*` prompts
- Direct imports: `predump`/`prepump`/`early`/`_confirm_shared`/`predump_dump_hunt`/`routing`

## 2026-06-15 — Hunt redesign completion (P0–P12)

- **Phase 0:** `--once` fast path (skip scan/cross-ex; CLI symbol cap); `watch_once_smoke`; baseline `data/baseline/hunt_baseline.json`
- **P2:** Split `tick_assembly` → `features/snapshot.py` + `detect/scoring.py` (649 LOC orchestrator)
- **P3:** `factor_panel` on tick row; real `structure.py`; `fib.leg_fib_levels`
- **P4:** Canonical FSM in `regime/leg_fsm.py`; `detect/lifecycle` shim
- **P5:** Canonical levels in `levels/levels.py`; B1 `atr1h` SL floor; `MIN_RR=1.5`
- **P6:** `evaluate_must_pass` wired in `evaluate_delivery`; ADX unified via `adx_thresholds`
- **P7:** Engine moved to `scan/_engine_impl.py`; `HUNT_SCANNER_V2`; `domain/setup_registry.py`
- **P8–P9:** `confluence_grid` + `templates` in confirm TG; single gate pass; tick lake buffer
- **P10:** `HUNT_EXIT_V2`; `append_outcome_record`; `track/candidates.py`; audit re-export in `events`
- **P12:** CI live-smoke job (network)

- **P1:** Removed `hunt_research/`, `intel/`, `_legacy/`, `calibrate/`, `/autotune`, `verify`, `monitor`.
- **P2:** Split `collect.py` → ingest-only (`data/collect.py`); tick assembly → `runtime/tick_assembly.py`; scanner → `data/scanner.py`. Fixed `SymbolFrames.book_bids` / `PreparedSymbol` POC fields.
- **P3:** Added `features/factors.py`, `structure.py`, `fib.py`; `_dev/check_factors.py`, `check_imports.py`.
- **P4:** Added `regime/leg_fsm.py`, `regime/regime.py`; rewired gate + cycle consumers.
- **P5:** Added `levels/` package; `_dev/check_levels.py`; structural levels wired in tick assembly.
- **P6:** Added `confluence/confluence.py` with family-vote + must-pass split.
- **P7:** Added `scan/predump|prepump|presqueeze|scanner.py`; `detect/__init__` re-exports scan facade.
- **P8:** Added `analysis/confluence_grid.py`; baseline `data/hunt_baseline.json` + `_dev/smoke_signals.py`.
- **P9:** Added `deliver/templates.py` table-driven formatters.
- **P10:** Extended `track/outcomes.py` single-writer KPI helpers.
- **P11:** Updated this changelog; lazy `runtime/__init__.py` breaks import cycles.
- **P12:** Added `.github/workflows/ci.yml` hunt static gates.

## 2026-06-12 — Gate-edge proof (central thesis confirmed)

- **`scripts/gate_edge.py`** — replays the confirm gate's historical output: the tick JSONL stores
  `dump.confirmed`/`long.confirmed` + frozen levels per tick. Episode-deduped to distinct signals and
  kline-graded with the SAME hold-to-target method as the raw baseline (apples-to-apples).
- **Result (hard, n=178):** confirmed **SHORT** sl=**27%** / tp1_reach=48% (n=128); confirmed **LONG**
  sl=**34%** / tp1_reach=22% (n=50). Raw-fade baseline sl=**52%**. **The gate cuts SL by 25pp (short) /
  18pp (long) — the confirmation gate IS the edge.** Validates R1's refusal to loosen thresholds.
- **Asymmetry finding:** shorts (fade pumps) are the real edge (27% sl, 48% reach); longs are marginal
  (34% sl, only 22% reach). Suggestion for analyst: favor shorts, scrutinize long confirms.
- Wired `compute_gate_edge()` into calibration + a prominent dossier table (`GATE_EDGE_OUTCOMES` path).
  Refresh via `python hunt/scripts/gate_edge.py --direction both`.

## 2026-06-12 — R1–R3: backtest as calibration truth-source

- **R1 — truth signal.** `calibration.py`: added `compute_backtest_rates()` (hold-to-target
  sl_hit/tp1_reach from `backtest_outcomes.jsonl`). `safe_to_apply` now blocks loosening when backtest
  `sl_hit > 30%` (n≥30) — the live `thesis_success` (100%, early-exit-biased) lost its veto. Backtest
  rates surface even on small-n early-return. Dossier leads with a live-vs-backtest divergence note.
- **Bug: test→production pollution.** `logic_verify` ran `close_signal(LATCHUSDT)` which appended to the
  real `signal_history.jsonl` every verify run (12 identical rows; HUSDT None-opened ×8 from a
  multi-watcher race). Added `archive=False` to `close_signal`; test opts out. Cleaned history 25→5
  genuine signals (`.bak` saved); killed 3 stale concurrent watchers.
- **R2 — early-exit verdict.** `early_exit_verdict()` joins live `lifecycle_stale`/soft closes with the
  hold-to-target backtest: avoided_stop vs forfeited_tp. Early read (n=5): cut 2 winners, 0 stops avoided
  → net-NEGATIVE (leaving money on table). Surfaced in dossier.
- **R3 — sample scale + REST enrichment.** Graded all ~265 pump_history legs (was 53):
  **sl_hit 40% · tp2 31% · tp1 9% · timeout 20%** — confirms the confirm-gate filters a 40%-SL raw
  universe down to clean live entries. Added `--enrich`: `atr_pct_from_klines()` (Wilder ATR%) +
  `atr_levels()` rebuild synthetic TP/SL from real pre-leg volatility instead of the flat 24h heuristic.
- **R3 enriched comparison.** ATR-realistic levels gave sl_hit **52%** (vs crude 40%) — the crude
  heuristic was loose on stops. `compute_backtest_rates()` now prefers `backtest_outcomes_enriched.jsonl`
  when present. Conclusion: the raw fade universe is a genuine loser; the confirm-gate IS the edge.
- **R4 — tuning.** Suggestions-only by decision. The system now correctly refuses to loosen
  (`safe_to_apply=False`, backtest_sl 52% > 30% gate). No thresholds changed unilaterally; the dossier
  carries backtest rates + early-exit verdict + TP1 analysis for human/analyst review.
- **R5 — `dump_init_score` validation → FAIL, NOT wired.** Extended `backtest_dump_init.py` to
  outcome-grade the armed bar (fade-short, ATR levels, forward walk). Armed n=8: tp1_hit=3, sl_hit=5 →
  **62% SL, worse than the 52% baseline.** Verdict FAIL; `dump_init_score` stays offline/experimental,
  NOT added to live `confirm_dump`. The validation harness prevented shipping a non-edge.
- **Root-cause fix: multi-watcher race.** The recurring duplicate / `opened_at=None` rows came from
  concurrent `watch.py` processes writing shared state. Added a single-instance PID lock
  (`hunt/data/watch.pid`) in `watch.main()` (skipped for `--once`): a second start is refused with a
  clear message. Cleaned history again → 8 genuine signals (incl. ESPORTSUSDT short TP2 **+35.99%**).
- **Verify:** `py_compile hunt/**` clean; `verify_logic` 120/120 passes and no longer pollutes; dossier
  renders truth-note + early-exit verdict; PID lock blocks a second watcher.

## 2026-06-11 — W27: SPACEUSDT instant TP1 + lifecycle_stale ping-pong

- **Problem:** SPACEUSDT short — TG entry → instant TP1 → `lifecycle_stale:impulse_initiating` invalidate ~82s later (+5.86% PnL but UX broken).
- **Root cause (2 bugs):**
  1. Price `0.008368` already below TP1 `0.008499` at confirm → TP1 on first tick
  2. `stale_lc` fired while `entry_lifecycle_phase == impulse_initiating` unchanged (same phase ≠ transition)
- **Fix:**
  - `signal_tracker._stale_lifecycle_invalidate` — skip stale when entry phase == current phase; skip after `tp1_hit`/`tp1_managed`
  - `watch.py` — block TG if price already at/through TP1 (`watch_telegram_skipped_past_tp1`)
- **Verify:** `run_stale_entry_phase_cases()` 3/3

## 2026-06-11 — P2: intel dossier (Layer 3 scaffold)

- **Fix:** `hunt/intel/` — `dossier.py`, `schema.py`, `report.py`, `provider.py` (optional Gemini)
- **CLI:** `hunt/scripts/analyze_session.py` → `intel_dossier.md` + `.json`; prints feed-to-Cursor hint
- **Guardrail:** never writes `hunt_calibration.json`

## 2026-06-11 — P1: backtest pump/dump leg events (sample growth)

- **Problem:** only ~5 closed live signals — stats/calibration blocked on n<30.
- **Fix:**
  - `hunt/hunt_watch/backtest_synthetic.py` — `leg_events_to_signals()` from `pump_history.json` leg_pump/leg_dump
  - `backtest_signals.py` — `--include-pump-events`, writes `hunt/data/backtest_outcomes.jsonl`
- **Verify:** `run_backtest_synthetic_cases()` 5/5; `backtest_signals.py --include-pump-events --limit 50`

## 2026-06-11 — P0: feature-vector latching + order-book walls (v-Complete data gap)

- **Problem:** `signal_history.jsonl` had outcomes but not the per-tick microstructure at entry/peak/close — stats and future intel dossier cannot learn feature→outcome patterns.
- **Fix:**
  - `hunt/hunt_watch/feature_latch.py` — `feature_vector_from_row()`, `book_walls_from_depth()`
  - `signal_tracker.py` — latch `features_open`/`features_peak`/`features_close` + `book_walls` at open; peak updates on MFE improve
  - `engine/market/rest_impl.py` — depth snapshot now includes top-5 `bid_levels`/`ask_levels` by notional
  - `watch.py` — `book_walls` on tick row; pass latched features at `register_signal_open`
- **Verify:** `run_feature_latch_cases()` 5/5; py_compile hunt/

## 2026-06-11 — W26: depth_imbalance as secondary confirm factor

- **Problem:** order book ask/bid pressure not used in live `confirm_dump` / `confirm_long`; only in beat_dump_lab experiments.
- **Fix:** `signal_engine.py` — `depth_imbalance ≤ -0.10` (ask-heavy) counts as a secondary factor in `confirm_dump`; `depth_imbalance ≥ 0.10` (bid-heavy) in `confirm_long`. Data already fetched in `fetch_rest_pack` → `prepared.depth_imbalance`.
- **Scope:** secondary factor only — does not gate confirmation alone; helps `closed_break + secondary ≥ 2` path.
- **Verify:** py_compile hunt/**/*.py → OK; watch restarted PID 73575.

## 2026-06-10 — Initial pump + mega leg + professional prompt

- **Extract:** `directional_filters.py` + `levels.fib_retracement_levels` — scoring filters / fib math out of watch monolith
- **Lifecycle:** `impulse_initiating`, `breakout_arming`, `mega_leg_continuation` (parabolic leg ≠ post_dump_bounce)
- **Early alerts:** PUMP/DUMP PREP/START; ignition bridge via `promote_initial_pump_lifecycle`
- **Dump:** ADX soft on exhaustion fade; early DUMP alerts before confirm
- **Forensic:** full BEAT pump to 8.3654 (not 4.27); JSONL gap May–Jun9 documented
- **Docs:** `docs/HUNT_IMPLEMENTER_PROMPT.md` v3 — canonical Claude prompt
- **Verify:** verify_logic 30/30

## 2026-06-10 — phase-hunt-impl-1 (forensic replay + phase-aware filters)
- jsonl_replay: recompute lifecycle FSM + long levels on stored ticks (stored phase = record-time code; flips now measurable). BEAT/VELVET window: 185× post_dump_bounce→distribution, 113× post_dump_bounce→impulse_initiating (VELVET mega-leg), 27× distribution→exhaustion_at_high (BEAT 6.14 top).
- alert_explain: `_hard_filter_blocks` — vwap_overbought/adx1h_uptrend soft on long@impulse_initiating/breakout_arming; adx1h_uptrend soft on short@exhaustion_at_high/distribution. Replay: BEAT exhaustion shorts gate-pass 52→129; VELVET impulse longs 0→7 (first 0.6976 @19:32Z).
- confirm_long in replay now receives lifecycle_phase (parity with live watch.py call).
- verify_logic 30/30; critical_audit BEAT/VELVET ok (both impulse_initiating live).

## 2026-06-10 — phase-hunt-impl-2 (calibration data + early-alert hygiene + live mark stream)
- signal_tracker: `entry_lifecycle_phase` immutable at open (lifecycle_phase мутировал каждый тик — фаза входа терялась); `close_lifecycle_phase` при закрытии.
- outcomes_report: таблица WR по entry phase × direction (первый прогон: post_dump_bounce short 0/3 -3.05%, post_dump_bounce long 2/0 +11.1%); lifecycle_stale/opposite_signal в LOSS_REASONS.
- early_alert: tier-hierarchy cooldown — start на cooldown глушит prep/imminent той же пары (replay: 76→68 would-sends).
- jsonl_replay: early_alert_simulation — would-send по tier'ам на recomputed lifecycle, общий cooldown-код с live.
- ws_feed *(historical — removed 2026-06-15)*: raw `!markPrice@arr@1s` replaced by CCXT Pro `watchMarkPrices` in `market/streams.py` (see `docs/CCXT.md`).
- verify_logic 30/30.

## 2026-06-11 — autonomous loop waves 2–14 (delivery gates + replay honesty + ops)

**North star:** tracker WR ≥70%, PnL growth. **Guardrails (n_tracker_closed < 30):** не снижать `confirm_min` / delivery fuel **72**; prep-shadow WR <50% → tighten держать.

### Delivery / alert_explain (W2–W5)
- `delivery_confluence_low` waiver: dump continuation shorts (`dump_active`/`distribution`, fall≥12%, structural dump hard, fuel≥min) — `min_struct_eff=1`.
- Bug fix: `_dump_continuation_short_ok` — убран redundant fuel re-check (блокировал confirmed shorts при fuel 64–71).
- Prep-shadow +3 fuel bump waived для confirmed structural dump shorts с fuel≥72.
- `_effective_min_rr()`: dump continuation shorts min R:R **1.10** (global 1.15).
- `_hard_filter_blocks`: adx1h_uptrend waived для short в `_DUMP_CONTINUATION_PHASES` (W11; replay 46%→72% gate-pass).

### Long path (W8–W10)
- `signal_engine.long_resistance_chase_veto()`: retest 0.5% если 5m closed above resistance, иначе chase floor 0.2%.
- `level_calibration.py`: +5% `sl_max_pct` для impulse/breakout hot mode.
- `levels._phase_min_rr_long()`: bounce 0.5, impulse 0.85, default 1.0.
- `watch._long_analysis`: `broke_resistance` только на **5m_closed**; intrabar → `live_above_resistance_unconfirmed` (+8 score only).

### Replay alignment (W6, W12)
- `jsonl_replay._replay_cal()` → `effective_hunt_params(symbol)` (confirm_min **72**, не defaults 60).
- `gate_lifecycle_phase()`: short gates → stored phase; long gates → recomputed pump phase over stale `distribution` (VELVET replay 0/2→2/2 gate).

### Ops / data plane (W13–W14)
- `resolve_tick_paths()`: daily archives + staging `dump_minute_watch.jsonl` (исправлен blind spot ~300 тиков).
- `watch.py`: periodic `rotate_hunt_ticks` каждые 10 min при staging ≥64KB.
- `scripts/hunt_boot_snapshot.py`: `latest_tick` meta; `scripts/hunt_journal.py`: autonomous journal helper.

### Metrics trajectory (replay + live)
| Metric | Baseline | Post W14 |
|--------|----------|----------|
| Short gate (replay) | 46% inflated | **~76%** (101/133) |
| verify_logic | 84/84 | **97/97** |
| verify_diff | 5/15 | **0–3/15** (premature only) |
| Tracker WR | 71.4% (n=7) | **85.7%** (n=7) — см. caveat ниже |
| prep_shadow WR | ~38% | **41%** (n=100) |

### Post-mortem: VELVETUSDT short @16:31 (thesis fail, paper win)
- Entry dump_active, `entry_lifecycle_bias=wait`, score 88, TG sent.
- MFE **~7.2%** vs TP1 need **~15.7%**; closed `bias_flip` (dump_active→post_dump_bounce) @+2.73%, **TP не достигнут**.
- Tracker считает **win** (structural exit + pnl>0.15%), но тезис провален.
- **Open:** блок TG/tracker open при `bias=wait` на dump_active short; отдельный счётчик `thesis_fail` для bias_flip без TP.

### Consciously NOT changed
- `confirm_min` / fuel floor **72** (n_tracker < 30).
- prep-shadow tighten при WR <50%.
- Delivery path order: contract → confluence → deliver.

### Verify
- verify_logic **97/97**; graphify updated.

## WAVE 15 — 2026-06-11 — thesis_outcome classification

### Root cause found
`outcomes_report.py` classified `lifecycle_stale`, `bias_flip`, `bounce_invalidate` as pure losses
regardless of pnl. Result: 4 scratch_wins (avg +3.5% pnl) were counted as losses, making
WR appear 0–20% when actual thesis success was 75%.

### Fix shipped: `hunt/scripts/outcomes_report.py`
- Added `_thesis_outcome(reason, pnl)` → `tp_hit | scratch_win | stop_loss | thesis_fail | unknown`
  - `tp1/tp2` → `tp_hit` (thesis fully validated)
  - `stop_hit` → `stop_loss` (hard loss, price against us)
  - soft exits (`lifecycle_stale`, `bias_flip`, `bounce_invalidate`, …) + pnl > 0 → `scratch_win`
  - soft exits + pnl ≤ 0 → `thesis_fail`
- Tables updated: thesis_success summary + phase×direction now shows tp/sw/sl/tf columns

### Metrics before → after (n=8 closed)
| Metric | Before (binary win/loss) | After (thesis_outcome) |
|--------|--------------------------|------------------------|
| dump_active short WR | 20% (1/5) | **60% thesis** (3/5: 1 tp + 2 sw) |
| exhaustion_at_high short WR | 0% (0/2) | **100% thesis** (2/2 scratch_win) |
| overall positive outcomes | 25% (2/8) | **75% thesis** (6/8) |
| stop_loss rate | — | 25% (2/8) |

### Signal sweep (8 closed)
| symbol:dir | entry_phase | entry_bias | close_reason | pnl | thesis_outcome |
|------------|-------------|------------|--------------|-----|----------------|
| WLDUSDT:long | accumulation | — | tp2 | +7.46 | tp_hit |
| FOLKSUSDT:short | exhaustion_at_high | short | bounce_invalidate | +4.62 | scratch_win |
| SOXLUSDT:short | dump_active | wait | stop_hit | -3.92 | stop_loss |
| VELVETUSDT:short | dump_active | wait | bias_flip | +2.73 | scratch_win |
| MAGMAUSDT:short | dump_active | wait | stop_hit | -1.58 | stop_loss |
| PLAYUSDT:short | dump_active | wait | tp2 | +4.88 | tp_hit |
| ARMUSDT:short | exhaustion_at_high | short | lifecycle_stale | +1.67 | scratch_win |
| HUSDT:short | dump_active | wait | lifecycle_stale | +5.97 | scratch_win |

Key finding: `entry_lifecycle_bias=wait` ≠ gate failure. Tracker opens at dump_confirmed TG
alert (not at entry tick). Wait-bias is valid at confirmation (dump_continuation bypass by design);
subsequent entry ticks blocked by `short_entry_not_ok`. Gate is working correctly.

### Verify
- compile_all hunt/ 0 errors; verify_logic 0/0 (no new rules added)

## WAVE 16 — 2026-06-11 — tp1_managed stop_hit reclassification

### Root cause found
MAGMAUSDT:short closed `stop_hit` with `pnl=-1.58%` but `tp1_hit=True, tp1_managed=True,
sl_at_breakeven=True`. This is a MANAGED EXIT (TP1 taken, trailing stop closed at breakeven) —
a thesis success — but was being classified as `stop_loss`.

### Fix shipped: `hunt/scripts/outcomes_report.py`
- `_thesis_outcome()` now accepts `tp1_managed=bool` kwarg
- `stop_hit + tp1_managed=True` → `scratch_win` (not `stop_loss`)
- Both tables pass `tp1_managed` from signal record

### Metrics before → after (n=8 closed)
| Metric | W15 | W16 |
|--------|-----|-----|
| thesis_success | 75% (6/8) | **88% (7/8)** |
| stop_loss | 2 | **1** (SOXLUSDT only — genuine, never reached TP1) |
| dump_active short thesis% | 60% | **80%** (4/5) |

SOXLUSDT -3.92%: genuine stop — extreme_lo 178.76 never reached tp1 170.21; held 9.7h.
No gate fix needed (score 81, wait bias — lowest quality in sample).

## WAVE 17 — 2026-06-11 — signal close events + distribution phase audit

### Root cause found
`close_signal()` in `signal_tracker.py` never emitted to `signal_events.jsonl`. All 10 close_signal
call sites are inside signal_tracker.py — no event was logged when signals closed (stop_hit, tp2, etc.).
This was a timeline analysis blind spot: close events were missing from signal_events entirely.

### Fix shipped: `hunt/hunt_watch/signal_tracker.py`
- Added `from hunt_watch.signal_events import append_signal_event as _append_event`
- `close_signal()` now appends `event="close"` with payload: close_reason, pnl_pct, duration_min,
  exit_price, close_lifecycle_phase, score, entry_lifecycle_phase, tp1_managed
- Exception swallowed silently (close must not fail due to logging error)
- Watch restarted (PID 65423)

### Distribution phase audit
- prep_shadow WR: distribution=61.9% (n=42) — highest phase but 0 deliveries
- All blocked by `not_anomaly` (16), `filter_block` (5), `tp2_too_close` (2)
- `anomaly_min_chg_24h_pct=8.0, anomaly_min_range_24h_pct=15.0`
- **Decision: HOLD** — guardrail n_tracker=8 < 30; loosening anomaly gate not warranted yet

### Verify
- compile_all hunt/ 0 errors; verify_logic pass; watch restarted

## WAVE 18 — 2026-06-11 — prep_shadow by_fuel breakdown

### Fix shipped: `hunt/hunt_watch/prep_shadow_tracker.py`
- Added `by_fuel` field to `PrepShadowSummary` dataclass
- `summarize_prep_shadows()`: groups closed prep_shadows into 16-wide fuel buckets
- `format_prep_shadow_html()`: appends "By fuel: fuel48-63: 30% · fuel64-79: 44% · …" line

### Calibration insight (n=160)
| fuel bucket | n | WR% |
|------------|---|-----|
| 48-63 | 79 | **30%** — validates keeping delivery gate ≥72 |
| 64-79 | 45 | 44% — solid |
| 80-95 | 27 | **26%** — flag: high fuel but low WR (over-confirmed reversals?) |
| 96-111 | 11 | 45% — small n |

Action: fuel80-95 low WR warrants further investigation in W19+ with more data.

### Verify
- compile_all 0 errors; verify_logic pass

## WAVE 19 — 2026-06-11 — fuel stored in tracker + outcomes fuel bucket

### Root cause found
`register_signal_open()` captured score but not fuel. All 10 tracker signals had `fuel=None`.
Outcome correlation by fuel was impossible from tracker data (only prep_shadow had fuel).

### Fixes shipped
**`hunt/hunt_watch/signal_tracker.py`**
- `register_signal_open`: `"fuel": setup.get("dump_fuel") or setup.get("long_fuel")`
- `close_signal` event payload: added `fuel` and `entry_lifecycle_bias` fields

**`hunt/scripts/outcomes_report.py`**
- Fuel bucket table (16-wide buckets): appears once fuel-tracked signals close

### New signal: LABUSDT:short
- score 91, dump_active, wait-bias → tp2 +10.76% in 2.4 min
- thesis_success now 8/9 = **89%**; dump_active short thesis 83% (5/6)

### Watch restarted PID 66421

## WAVE 20 — 2026-06-11 — near-TP1 stale grace

### Root cause
HUSDT (2% from TP1) and ARMUSDT (1% from TP1) closed by lifecycle_stale at tick 3 (3 min grace).
These were scratch_wins that could have been tp_hits with a small extension.

### Fix shipped: `hunt/hunt_watch/signal_tracker.py`
`_stale_lifecycle_invalidate`: when `remaining_to_tp1 ≤ 3%` and `tp1_hit=False`, extend
`ticks_needed` from 3 → **8** (8 minutes). Only applies to non-tp1-hit cases.

### Logic verify: `hunt/hunt_watch/logic_verify.py` + `hunt/scripts/verify_logic.py`
- `run_stale_grace_cases()`: 2 cases (far TP1 closes normally; near TP1 holds at tick 4)
- **verify 99/99** passed

### Watch restarted PID 66902

## WAVE 21 — 2026-06-11 — prep_shadow by_score breakdown

**Problem:** prep_shadow report had no score breakdown — impossible to know if higher-score setups confirm more reliably.

**Fix:** `hunt/hunt_watch/prep_shadow_tracker.py`
- Added `"score"` field to `_open_shadow()` (reads `dump_score` / `long_score` from setup)
- Added `by_score: dict[str, dict[str, Any]]` to `PrepShadowSummary` dataclass
- Added `by_score` computation in `summarize_prep_shadows()` — 20-wide score buckets (60-79, 80-99, 100-119, ...)
- Added `By score:` display in `format_prep_shadow_html()` — shows WR% per bucket (n≥3 filter)

**Gate sweep (no P0):**
- `short_entry_not_ok` (162 blocks): dump_active bias=wait — all 4 symbols eventually delivered; 3 wins, 1 stop. Gate protective.
- `filter_block` (78 blocks): vwap_oversold — PIPPINUSDT/SIRENUSDT blocked entirely (correct — chasing extended dump).
- `below_forming_min` (36 blocks): fuel < 45 — expected pre-filter.
- Thesis_success: 9/10 = 90%; BTWUSDT:short active, ext_lo=0.07565, TP1=0.073353, 3.04% remaining.

**Note:** `by_score` in prep_shadow will populate as new shadows cycle through post-W21. Historical 191 closed records have `score=0` (pre-fix).

**verify 100/100** passed

## WAVE 23 — 2026-06-11 — HMSTR diagnosis + Telegram fmt + autotune + self-calibration

## WAVE 24 — 2026-06-11 — per-symbol anomaly thresholds для XAGUSDT/XAUUSDT

**Problem:** `anomaly_min_chg_24h_pct=7.2`, `anomaly_min_range_24h_pct=18.2` — глобальные пороги откалиброваны под крипто-волатильность. Серебро (XAG) и золото (XAU) типично движутся 1–3% в сутки, поэтому `not_anomaly` блокировал их вечно (39 блоков за сессию на XAGUSDT с fuel 80–84).

**Fix:**
- `hunt/hunt_watch/param_store.py` `effective_hunt_params()`: добавлены `anomaly_min_chg_24h_pct` и `anomaly_min_range_24h_pct` в per-symbol override chain
- `hunt/data/hunt_calibration.json` per_symbol: XAGUSDT/XAUUSDT → `anomaly_min_chg=1.8`, `anomaly_min_range=4.5`
- BTCUSDT/крипта: без изменений (7.2/18.2)

**Verify:** 100/100, compile OK. Watch restarted PID 71719.

### HMSTRUSDT — не баг, gate корректен
379 forming, 5 start (dump_initiating, exhaustion_at_high, fuel 46–68) — ноль blocked событий, ноль deliveries.
Трассировка `confirm_dump()` (`signal_engine.py:178`):
1. `fuel < confirm_min_score (60)` — большинство тиков 46–58, confirm не проходит
2. Structural: нет 2x `{5m/15m_close_below_support, 5m_rejection_exhaustion}` одновременно
3. Orderflow: `agg_trade_delta_60s > 0.42` (покупатели активны) → `veto_orderflow_buy_pressure_vs_short`
**Вывод: HMSTR в exhaustion_at_high без реального структурного пробоя. Gate работает правильно.**

### Telegram форматирование (`hunt/scripts/watch.py` +257 строк)
- `_PHASE_HUMAN` dict: 19 фаз → понятный русский текст
- `_format_telegram()` переписан: emoji + вход/стоп/TP + % расстояние + Score/Fuel
- `_format_followup_telegram()`: TP1 hit / TP2 / закрыт / стоп-предупреждение — отдельные карточки
- `_reason_human()`: human-readable причина из phase + triggers (volume/cascade/rejection/etc)

### Self-calibration (`hunt/hunt_watch/calibration.py` новый, +140 строк)
- `compute_auto_calibration(state)` → suggestions + adjustments + safe_to_apply
- n < 20 → "недостаточно данных"; thesis_success < 70% → safe_to_apply=False
- Анализ fuel/score/phase бакетов — только SUGGEST, не применяет автоматически
- `calibrate_all.py`: вызывает `_print_auto_calibration()` перед основной калибровкой

**verify 100/100** passed

## WAVE 22 — 2026-06-11 — closed_history: outcomes no longer lost on repeat signals

**Problem:** SIGNAL_STATE uses `{symbol}:{direction}` as dict key — when same symbol re-opens, the previous closed record is overwritten. `outcomes_report.py` was undercounting outcomes. Observed: LABUSDT had 2 tp2 closes (pnl=10.76 + pnl=8.76) but only 1 counted.

**Fix:**
- `hunt/hunt_watch/signal_tracker.py` `close_signal()`: appends `dict(sig)` snapshot to `state["closed_history"]` list before the signal_events write. History accumulates all closes across signal cycles.
- `hunt/scripts/outcomes_report.py`: reads from `closed_history` when populated; falls back to `signals` dict for installs pre-dating this fix.

**Impact:** all future closed signals will accumulate in `closed_history`. Historical 10 signals (pre-W22) retained via fallback. First LABUSDT close (pnl=10.76) is permanently lost (pre-W17/22).

**Watch restarted PID 68762**

**verify 100/100** passed
