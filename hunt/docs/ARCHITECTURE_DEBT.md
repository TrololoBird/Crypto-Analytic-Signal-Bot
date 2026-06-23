# Hunt — architectural debt register

Tracked **separately** from logic-redesign phase completion (`IMPLEMENTATION_STATUS.md`).
P0–P9 delivered the redesign scope; items below are known gaps, drift risks, and follow-on work.

## P0 — dual confirm model (stale `_ev` path)

**Problem:** `[confirm.short] min_score` still exists in TOML and `gate/_ev.setup_meets_strength` references it. Hot path uses `detect/setup_fields.setup_meets_strength` → `confirmed` only.

**Risk:** operators tune `min_score` expecting TG changes; developers import wrong `setup_meets_strength`.

**Target:** deprecate `_ev.setup_meets_strength`; TOML comment `reporting_only` (done); remove from threshold matrix as delivery authority.

**Status:** documented in [`AUTHORITY_MODEL.md`](AUTHORITY_MODEL.md) §1; code cleanup open.

---

## P0 — config drift (scanner thresholds)

**Problem:** `data/scanner.py` still hardcodes candidacy floor (`score < 25.0`) while watchlist/priority read `[scanner]` from `config.defaults.toml` / env. Tuning TOML alone can leave candidacy filter on a different value than operators expect.

**Risk:** `score_watch = 50` in config but `45` behavior in code (or vice versa) after partial edits.

**Target:** single source of truth — scanner reads all `[scanner]` thresholds from `BotSettings` / `params/store.py`; delete duplicate literals.

**Status:** open.

---

## P1 — authority block funnel (diagnostics)

**Problem:** Many layers can block delivery (fusion, mission, playbook, RR, EV, must_pass, family_vote, arbiter). `authority_audit` validates **delivered** rows only; it does not rank **which layer blocks most** or whether blocks improve outcomes.

**Target:** extend `_dev/authority_audit` (or sibling report) to emit per-session:

| Field | Meaning |
|-------|---------|
| `fusion_pass` | `gate_open` / `setup.confirmed` |
| `playbook_pass` | N-of-M |
| `mission_pass` | pre_* phase lock |
| `rr_pass` / `ev_pass` | geometry gates |
| `block_layer` | first failing gate in canonical order |

Build block-rate histograms from `setup_candidates.jsonl` + `hunt_outcome_ledger.jsonl`.

**Status:** open (design agreed; tooling partial — ledger flags exist, funnel stats not built). Spec: [`AUTHORITY_MODEL.md`](AUTHORITY_MODEL.md) §9–10.

---

## P1 — factor coverage floor (`min_active_factors=2`)

**Problem:** gate can open with 2/6 directional factors (33% coverage).

**Target:** evaluate `min_active_factors=3` or coverage ratio after `replay_fusion` + live funnel.

**Status:** documented; default unchanged.

---

## P1 — alternate-side telemetry

**Problem:** fusion emits one side; close rank votes not persisted for calibration.

**Target:** stamp `n_pos`, `n_neg`, `rank_margin`, full `fusion.parts` on every `hunt_scan` row.

**Status:** open.

---

## P1 — unified Signal Ledger (pre-TG → tracker)

**Problem:** Confirm-pass + TG-fail is visible in `dump_minute_watch.jsonl` / `setup_candidates.jsonl` but **not** in `track/tracker.py` FSM (tracker opens only after `telegram_sent=True`).

**Target lifecycle (single audit trail):

```text
forming → confirmed → delivery_attempt → delivery_failed | telegram_sent → tracking → closed
```

**Status:** open — documented gap in `HUNT_ARCHITECTURE.md`; implementation not started.

---

## P2 — tier vs fail-closed ambiguity

**Problem:** `strict_data_quality=True` blocks ticks with missing derivatives, while **fast tier** intentionally skips derivatives REST. Docs did not spell out per-tier expectations.

**Resolution:** canonical matrix in `HUNT_ARCHITECTURE.md` § Tier capability matrix. Code behavior unchanged until fast-tier policy is revisited.

**Status:** documented; code policy review optional.

---

## P2 — god-objects (>1000 LOC)

Large modules remain merge-policy files (not split targets): `runtime/cycle/_impl.py`, `gate/delivery.py`, `deliver/telegram.py`, `track/tracker.py`, `contract.py`, `market/client.py`, `market/streams.py`, `runtime/tick_assembly.py`.

**Risk:** regression surface on calibration / delivery edits.

**Status:** accepted debt; split only when correctness or testability forces it.

---

## P2 — event-driven backtest

No full replay of watch tick → gates → delivery on historical bars. Available: `replay_fusion` (detection), `reconcile_signals` (post-hoc TP/SL), live smoke.

**Status:** open.

---

## P2 — TG-fail observability

Delivery errors may not surface in tracker follow-ups. Operators must correlate `hunt_outcome_ledger.jsonl`, `setup_candidates.jsonl`, and Telegram transport logs.

**Status:** open; subsumed by Signal Ledger target.

---

## Compatibility stubs (not “zero legacy”)

Legacy **detection stack** removed (2026-06-20). These **compat shims** remain for import stability and removed formatter hooks:

| File | Role |
|------|------|
| `detect/legacy_compat.py` | stubs for deleted scan formatters |
| `probe_compat.py` | probe/dev harness aliases |
| `scan/scanner.py` | thin shim → `detect/routing` |

Do **not** describe the system as “zero legacy” — use **“legacy detection removed; compat stubs retained.”**

---

## Related docs

- **Authority model (canonical):** [`AUTHORITY_MODEL.md`](AUTHORITY_MODEL.md)
- Canonical pipeline: `HUNT_ARCHITECTURE.md` § Delivery authority
- Authority map: `AUTHORITY_MAP_v2.md`
- Implementation phases: `IMPLEMENTATION_STATUS.md`
