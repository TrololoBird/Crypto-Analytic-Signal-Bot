# OPEN_DECISIONS — operator must resolve before destructive migration

Date: 2026-06-21. **Nothing destructive happens until these are answered.** Each item:
the question, options with trade-offs, and a recommendation (not a decision). The
`MIGRATION_PLAN.md` will be written only after these are resolved.

---

## A. (§5) Canonical detection core for Module 2 — THE key choice
Five producers exist (AUDIT §2.3). Pick the core; the rest become inputs or are deleted.

| Option | Core | Becomes input | Delete | Trade-off |
|--------|------|---------------|--------|-----------|
| A1 **(recommended)** | `detect/fusion` (phase + factors + self-calibrated gate) | `maps/forecast` bands, `expansion` blocks as factor scores, `manipulation_fusion`/playbook as a confluence input | `setups/catalog` EV-bootstrap delivery path (keep detectors as features) | Fusion is already trailing-clean, abstain-on-missing, score≠prob, replayable. Smallest leakage surface. Loses expansion's rich 24-block taxonomy unless ported as inputs. |
| A2 | `expansion_engine` (24 blocks + learning/calibration) | fusion factors, maps bands | catalog, manipulation_fusion | Richest feature set + built-in calibration/learning. But largest LOC, more god-objects, less verified for look-ahead; would re-home the fusion gate. |
| A3 | Hybrid: fusion gate + expansion blocks as the factor bank | maps, playbook | catalog | Best of both, most integration work; risk of re-introducing multi-authority. |

**Recommendation: A1**, porting the most predictive expansion blocks as fusion factors
incrementally (measured against the ledger), not wholesale.

## B. (§3.1) Canonical pinned set
Defaults = 4 (`BTCUSDT,ETHUSDT,XAUUSDT,XAGUSDT`); docs also cite 7 (+SOL,XRP,PAXG) and a
panel with 1w only for BTC/ETH/XAU/XAG.
- **B1 (recommended):** 4 anchors as canonical default; SOL/XRP/PAXG opt-in via runtime
  `config.toml`. Document why metals/PAXG get reduced higher-TF data (low 1w history).
- **B2:** Promote 7 to default. More coverage, more Full-tier cost on thin 1w history.

## C. (§4.1) "Pre" vs closed-bar lag — **design jointly with G**
- **C1 (recommended, coupled to G1):** Two-stage future-signal — emit ARMED (limit-in-zone
  plan) on pre-move evidence, then TRIGGERED only on closed-bar confirm. **The "evidence"
  for ARMED must be computed on the last *closed* bar (G1), not the forming bar** — otherwise
  "pre-move without confirm" is just legalized look-ahead and re-opens the §2.1 leak. C1 and
  G1 are one design, not two. (Re-introduces an armed lane that dispatch retired —
  `dispatch.py:254-265`.)
- **C2:** Keep confirm-only (status quo); accept missing intrabar memecoin moves.
- **C3:** Lower confirm TF further; raises wash risk (see E).

## D. (§4.2) Capacity vs market coverage
`max_dynamic_symbols=12`, scan 900s, watch 30s.
- **D1 (recommended):** Tiered cadence — keep 12 Full-tier watched + a cheap Lite prescan
  (debounced) over the full universe at ~60–120s for ignition, promoting hits into the 12.
- **D2:** Raise `max_dynamic_symbols` (more WS/CPU load, rate-limit risk).
- **D3:** Status quo; accept the 15-min scan blind spot.

## E. (§4.3 + §4.5 + §2.10) Exploratory vs production delivery & wash on thin alts
EV bootstrap delivers to production TG by default (`catalog.py:173`); `dump_fast_confirm`
lowers the bar on thin alts; z-scores are computed before any wash gate.
- **E1 (recommended):** Split channels/tags/ledgers — exploratory (EV bootstrap, fast
  confirm) to a separate "lab" channel + ledger; production north-star measured only on the
  confirmed lane. Move a wash/spoof gate **upstream** of factor z-scoring (or compute z on a
  wash-filtered window). Set `HUNT_EV_BOOTSTRAP` default off in production.
- **E2:** Keep one channel; tag exploratory inline. Simpler, still pollutes north-star.

## F. (§4.4) Longs
Doc says "longs off until n≥30"; **code does not hard-disable longs** (NOT CONFIRMED —
`SniperConfig.live_phases_long` populated, `dispatch.py:378`).
- **F1 (recommended):** Decide the *intended* policy. If longs should be accumulated-but-
  not-delivered until n≥30 long outcomes, implement an explicit deliver-gate that still
  writes blocked-long rows **with geometry** to the ledger (fixes §2.11 for longs), then
  ramp by outcome count. If longs are meant to be live now, update the docs.
- **F2:** Leave as-is (longs live), accept survivorship until enough outcomes.

## G. (§2.1) Forming-bar provenance — **foundational, not independent**
- **G1 (recommended):** Add a closed-bar assertion/flag on `current_vector` at
  `build_live_detection` (`detect/live.py:15`) so a forming bar can never be z-scored as
  closed; document the contract. **This is a precondition of A1 and C1**: every fusion
  z-score (`calibrate.py:66`) and the phase CUSUM (`phase.py:67`) read the window's last row;
  if it can be a forming bar, the entire detection core (= Module 2 under A1) and any
  pre-confirm ARMED emission (C1) ride on an unclosed bar. Must land before/with the
  structural work, not "whenever".
- **Open sub-question to verify:** does `build_feature_vector(prepared, …)`
  (`tick_assembly.py:1007`) build its 15m last row from a closed or forming bar? Not yet
  traced — the `[INFERENCE]` in §2.1 stands until this is read.

## H. (§2.8) Single source of truth for constants
- **H1 (recommended):** Make `data/scanner.py` `ScanConfig` and the `[fusion]` keys read
  from `config.defaults.toml`, so there is one source. **Priority sub-fix (verified
  2026-06-21):** `[fusion] lookback`/`q_gate`/`q_phase` are currently **inert** — the live
  path uses code constants `240`/`0.90`/`0.90` and ignores the TOML (`lookback=120`,
  `q_gate=0.92`, `q_phase=0.85`). Either thread `fusion_params()` into
  `build_live_detection`/`build_detection` defaults (pass `None` so `_fp()` wins) or delete
  the inert TOML keys. Until then, tuning those three keys does nothing — an active
  operability trap.

## I. (NEW — §2.11 + A1 dependency) Block-row geometry — **gating precondition for A1**
- The ledger omits entry/SL/TP on block rows (§2.11). A1's plan ("port expansion blocks as
  fusion factors, **measured against the ledger**") therefore selects blocks on a
  survivorship-biased signal **in both directions** (not just longs — my earlier short-only
  framing was wrong).
- **I1 (recommended):** Persist full geometry (entry_zone/SL/TP1/TP2) on **every** ledger
  row — deliver *and* block, long *and* short — in `build_ledger_record`
  (`outcome_ledger.py:86-136`), so counterfactual replay is possible from the ledger, not
  only from the raw parquet lake. Land **before** A1's incremental block-porting begins.

---

## Resolve-order (revised — data integrity precedes structure)
The original "A→B→E gate; C,D,F,G,H independent" understates dependencies. Corrected:

1. **Data-integrity block (do first):** G1 (closed-bar provenance) · I1 (block-row geometry,
   both directions) · E1's wash-before-z. A1 and C1 are built on these; running A1's
   ledger-measured block selection before I1/G1 means tuning on biased, possibly
   look-ahead-contaminated data.
2. **Structure:** A (core) · B (pinned).
3. **Independent, any time:** D (capacity), F (longs policy), H (config source — but the
   inert-keys sub-fix is cheap, do it early).
4. **Coupled:** C1 designed together with G1.

Answer **A, B, E** to unblock structure; authorize **G1 + I1** as the early data-integrity
fixes.

---

## Unverified foundations (NOT yet read in code — do not assume true)
TARGET_ARCHITECTURE asserts these as the basis of module independence; AUDIT did **not**
verify them. They need a code pass before the structural migration is trusted:

- **U1 — "each 0A raw fact computed once".** Claimed in TARGET; not verified. If
  `price/volume/oi/funding/delta/atr/poc/hvn/lvn` are recomputed independently inside
  fusion, expansion, catalog, and maps planes, the "shared facts / independent
  interpreters" boundary does not actually exist yet and is itself migration work, not a
  given. **Verify before relying on it.**
- **U2 — MTF aggregation semantics.** TARGET stores facts per `(symbol, tf)` (the *form*),
  but where cross-timeframe alignment is decided (e.g. `fractal_alignment` — "1D/4H/1H
  aligned") is unmapped. For a pre-move scanner this is detection logic, not plumbing.
  Determine whether it is computed deterministically in one place or smeared across planes.
- **U3 — Lite/Full tier cleanliness.** TARGET says "tier already exists as `snapshot_tier`,
  formalize". Not verified that the current split is clean (no Full-only field leaking into
  the Lite scan path). Confirm before treating it as the module-data contract.

## J. (NEW) Cross-module conflict — confirm intentional
"One arbiter per module" resolves in-module conflicts but **not** Module 1 (e.g. WAIT) vs
Module 2 (e.g. PRE-PUMP) firing on the same symbol — e.g. when the operator sends a scanner
candidate into deep analysis. Per the brief, modules are independent, so simultaneous
two-module messages on one symbol are presumably **expected, not a bug**. Confirm this is
intended (and decide whether the two messages should at least cross-reference each other),
so it is not "fixed" later as an accidental duplicate.
