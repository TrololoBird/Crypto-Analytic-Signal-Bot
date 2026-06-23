# Authority model (code-verified)

Canonical resolution for architecture audit **2026-06-23**. When docs disagree with this file, **this file + code win** until explicitly changed.

Related: [`HUNT_ARCHITECTURE.md`](HUNT_ARCHITECTURE.md) · [`ARCHITECTURE_DEBT.md`](ARCHITECTURE_DEBT.md) · [`AUTHORITY_MAP_v2.md`](AUTHORITY_MAP_v2.md)

---

## 1. Two confirm models — only one is production authority

| Mechanism | Config / module | Production TG? | Role |
|-----------|-----------------|----------------|------|
| **`setup.confirmed`** | `detect/result.py` → `gate_open` | **YES** | Sole confirm authority on hot path |
| **`[confirm.short] min_score`** | `config.defaults.toml`, `params/store.py` | **NO** | Operator `/signals` gaps, `gate/_report.py`, stats cards |
| **`setup_meets_strength` (fusion)** | `detect/setup_fields.py` | **YES** | `return bool(setup.get("confirmed"))` |
| **`setup_meets_strength` (_ev)** | `gate/_ev.py` | **NO** (stale) | Duplicate; still references `confirm_min_score` — **deprecate** |

**Who wins:** `setup.confirmed` from fusion. `confirm_min_score` **cannot** promote a setup to confirmed and **does not** block delivery when `confirmed=True` on the hot path (`_cycle_tick`, `_cycle_confirm`, `track/candidates` all import `setup_fields`).

**Risk:** operators tuning `[confirm.short]` expecting TG behavior change — **wrong knob**. Tuning fusion: `[fusion] q_gate`, `global_gate_floor`, `min_active_factors` ([`FUSION_PARAMS.md`](FUSION_PARAMS.md)).

**Debt:** remove or alias `_ev.setup_meets_strength` → `setup_fields`; mark `[confirm.short]` as `reporting_only` in TOML.

---

## 2. Single-side fusion — not dual long/short scores

Fusion runs **once** per symbol per bar (`build_live_detection` → `fuse` → one `side`).

```text
factors → signed median → side ∈ {long, short, none}
→ to_setup_dict: ONE active setup; opposite side = empty stub (confirmed=False)
```

`watch_tick` `long_score` / `short_score` are **not** independent hypotheses:

- Active side: `fusion_score` from detection
- Stub side: `0` (no setup)

**Not lost (for calibration):** `triggers`, `gate_reason`, `n_active`, `agreement`, `z_dir`, per-factor scores on the active setup; `setup_candidates.jsonl` + `dump_minute_watch.jsonl`.

**Lost:** explicit alternate-side score when rank vote is close (e.g. 4 factors long-ish vs 3 short-ish). **Debt:** persist `fusion.parts`, `n_pos`/`n_neg`, `rank_margin` on every tick for offline analysis.

`route_tick` does not collapse two confirmed setups — fusion never emits two `confirmed` sides on one bar.

---

## 3. `fusion_score` vs `gate_open` — different decisions

| Field | Meaning | Drives TG? |
|-------|---------|------------|
| `fusion_score` | 0–100 **strength index** (`magnitude × scale`) | No |
| `gate_open` / `confirmed` | quantile pass ∧ agreement ∧ min factors ∧ phase `watch_ok` (+ prep override) | **Yes** |

**Explainability rule:** UI/logs must show **`gate_reason`** alongside `fusion_score` when `confirmed=False`. High `fusion_score` with closed gate is valid (`below_calibrated_gate`, `phase_block:mid`, `insufficient_factors:1`, …).

Delivery cards should prefer: `confirmed` + `gate_reason` + `fusion_score`, not score alone.

---

## 4. Backtest gap — methodological risk

| Tool | Covers |
|------|--------|
| `replay_fusion` | Detection parity: `gate_open`, phase, `fusion_score` |
| `live_integrity_check` | One-shot tick → route → gates |
| `reconcile_signals` | Post-hoc TP/SL after tracker open |

**Missing:** `fusion → delivery gates → telegram → tracker → outcome` on historical bars.

Calibrating playbook / mission / RR / EV without this path is **partial**. Tracked as P2 in [`ARCHITECTURE_DEBT.md`](ARCHITECTURE_DEBT.md). Not solvable by doc edit alone.

---

## 5. Factor coverage — explicit floors

From `detect/config.py` / `fusion.gate()`:

| Rule | Default | Effect |
|------|---------|--------|
| `min_active_factors` | **2** of 6 directional | `<2` → `gate_open=False`, reason `insufficient_factors:N` |
| `agreement` | rank vote vs median sign | mismatch → `factor_disagreement` |
| `abs_magnitude_floor` | 0.5 (vol-adjusted) | below → `below_abs_floor` |
| `q_gate` + `global_gate_floor` | 0.92 / 0.55 | symbol quantile gate |

**33% model coverage:** with exactly 2/6 factors active, gate **can** open if quantile + phase pass. This is **by design** today — not documented as safe.

**Debt (P1):** consider `min_active_factors=3` or `min_coverage_ratio=0.5` after replay evidence; until then operators must treat `n_active=2` ticks as low-confidence.

**Tick block vs factor abstain:**

- `strict_data_quality=True`: blocks tick when **required assembly keys** missing for tier
- Factor `active=False`: tick proceeds; fusion may still fail gate from insufficient active factors

---

## 6. Fast tier vs delivery — resolved rules

| Question | Answer |
|----------|--------|
| Fast tier skips heavy derivatives REST? | Yes (`data_readiness`: `fast_tier_derivatives_skipped`) |
| Can fast tier deliver? | **Yes**, if `DELIVERY_MARKET_KEYS_FAST` present (OI, funding, taker z-scores, …) — often from WS/cache |
| Full tier extra keys? | `oi_chg_5m`, `ls_1h`, … (`DELIVERY_MARKET_KEYS_FULL`) |
| Production lane without derivatives? | **No** — `delivery_derivatives_complete(row, tier=...)` blocks |

Fast tier = **capacity optimization**, not permission to ship with empty `market.*` derivatives.

---

## 7. Scanner recall → Fusion precision

```text
hunt_score (scanner)  →  universe membership  →  fusion sees symbol or not
fusion (detect/*)     →  side + confirmed      →  delivery gates
```

**Hidden hierarchy:** scanner errors are **recall** errors (fusion never runs). Fusion errors are **precision** errors (confirmed wrong or blocked).

| Stage | Role | Analogy |
|-------|------|---------|
| Scanner `hunt_score` | Who enters the watch universe | Recall |
| Fusion `gate_open` | Who gets a directional candidate | Precision |
| Delivery gates | Who reaches Telegram | Policy |

Document as **fusion-driven precision on scanner-filtered recall**, not “fusion-only pipeline”.

---

## 8. Unified lifecycle FSM (cross-module)

Official states and where they live:

| State | Module | Meaning | TG |
|-------|--------|---------|-----|
| `forming` | tick / bar | Open bar or pre-gate telemetry | silence |
| `gate_open` (internal) | `detect/fusion.py` | Magnitude quantile pass (pre-phase) | — |
| `confirmed` | `setup.confirmed` | `gate_open ∧ watch_ok` (+ prep override) | candidate |
| `armed` | scanner card / coil | Near trigger; coil bracket; advisory | ARMED card |
| `signal` | `signals/` spine (Deep) | Emitted thesis | once |
| `activated` | Deep `activation.py` | In zone / fill | once |
| `delivery_attempt` | `deliver/dispatch.py` | Arbiter passed, transport called | — |
| `delivery_failed` | transport | TG error (**not in tracker FSM**) | — |
| `tracking` | `track/tracker.py` | `telegram_sent=True` | follow-ups |
| `closed` | outcomes | TP/SL/expiry | outcome |

`setup.confirmed` (Scanner) ≠ `signal` (Deep spine) — same word family, different modules.

---

## 9. Veto layers — ordered, auditable

Production sequence (first failure wins for attribution):

```text
1. fusion (not confirmed)           → no route_tick candidate
2. contract + must_pass + family_vote
3. mission (pre_* lifecycle lock)
4. playbook N-of-M
5. RR geometry / min_rr
6. EV / p_win (when HUNT_PWIN_GATE=1)
7. freshness / wash / kinematic / data completeness
8. arbiter (fusion + playbook + mission snapshot)
9. telegram transport
```

**Today:** `hunt_outcome_ledger.jsonl` + `setup_candidates.jsonl` record blockers; `authority_audit` checks **delivered** invariants only.

**Debt (P1):** block-layer histogram — count first `GateResult.code` per symbol-tick; correlate with outcomes.

---

## 10. Direction authority vs Lifecycle authority (core tension)

| Authority | Owner | Decides |
|-----------|-------|---------|
| **Direction** | `detect/fusion.py` | long / short / none from factor median + rank vote |
| **Timing window** | `detect/phase.py` | `watch_ok`: PRE phase matches side; MID closes gate |
| **Mission lock** | `gate/_mission.py` | Delivery only in pre-dump / pre-pump vocabulary |

**Observed conflict (by design):**

```text
Fusion: high magnitude, side=long, phase=mid  → gate_open=False (phase_block)
Mission: mid_leg block at delivery            → redundant if gate already closed
```

Opposite case (user logs):

```text
Fusion: continuation signal, score high
Mission: mid / late leg → blocks TG
```

Both can be “correct” locally; **outcome = no signal**. This is the main explainability gap for “market moves but bot silent”.

**Mitigation (doc + ops):**

- Log `gate_reason` + `mission` block in same tick row (already in `watch_hunt_skipped`)
- Authority funnel stats to measure **phase_block vs mission_block vs playbook_block**
- Do not tune fusion expecting mid-leg delivery — mission is intentional product policy

---

## Authority audit (next implementation)

Extend `_dev/authority_audit` or add `_dev/authority_funnel.py`:

```text
Per blocked candidate:
  fusion_pass, playbook_pass, mission_pass, rr_pass, ev_pass, first_block_code
```

Run after live sessions; output JSON summary for calibration loops.
