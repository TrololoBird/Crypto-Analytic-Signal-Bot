# Hunt engine — design spec (first principles)

> **Status:** canonical design intent (2026-06-19, Pass A sync).  
> **Mission:** find setups **before** the move — manipulation formations, not mid-leg chase.

## 1. What we trade

**Universe:** Binance USDⓈ-M perps, meme + anchors (BTC/ETH/XAU/XAG).  
**Edge:** structural futures — liquidity sweeps, BOS/CHoCH, POC/VAH/VAL, OI/funding, maps.  
**Horizon:** closed-bar confirm → manual Telegram entry; no auto-trade.

### Three archetypes (TO-BE)

| Archetype | Lifecycle window | Forecast |
|-----------|------------------|----------|
| **predump_short** | exhaustion / distribution / dump_initiating | `build_dump_forecast` ↓ |
| **coil_long** | accumulation / breakout_arming | `build_maps_forecast` ↑ |
| **ignition_long** | squeeze setup (not mid-pump) | `build_ignition_forecast` ↑ |

**Playbook N-of-M** is the sole delivery authority when `HUNT_PWIN_GATE=0` (default).  
**ManipulationFusionScore** ranks candidates via the same checklist (`primary_score = pass_count/required_n × 100`); weighted domain scores are display-only and must not gate delivery.

### Watch auto-scan (TG confirm-only)

Delivers only when the move is **imminent**, not underway. PRE-phase filter runs **before** confirm (`hunt_skip_reason` in tick assembly + confirm layer); mission gate is defense-in-depth only.

| Mission | Lifecycle window | Intent |
|---------|------------------|--------|
| **Pre-dump short** | `exhaustion_at_high`, `distribution`, `dump_initiating` | Dump **about to** start |
| **Pre-pump long** | `accumulation`, `breakout_arming`, `post_dump_bounce`, `recovery` | Pump/bounce **about to** start |

**Never hunt/confirm on watch:** `dump_active`, `impulse_initiating`, `mega_leg_continuation`.

### Query plane (`/signal`, pinned, user symbol)

**Deep Analysis** (`analysis/deep/`): pinned indicator panel + MTF + maps forecasts + structure-first verdicts. Watch `would_deliver` is an optional appendix only — not the verdict driver.

---

## 2. Three planes (runtime architecture)

```
INGEST → DECISION (features → fusion → lifecycle → gate → contract) → QUERY + DELIVERY
```

- One `MarketPlane` per process.
- Watch is the **only writer** of materialized rows.
- `/signal` is **query-first** (store → format); REST on miss/stale/`--live`.
- Delivery path: `validate_signal_contract` → gates (incl. mission, squeeze predump) → `deliver`.

---

## 3. Decision stack (order matters — post-fusion 2026-06-20)

**Single authority chain for watch TG:**

```text
data_readiness (block tick)
  → fusion: factors → fuse → CUSUM phase → magnitude gate  ⇒  gate_open / confirmed
  → geometry: levels.py (entry/SL/TP — not a gate)
  → validate_signal_contract + must_pass + family_vote
  → delivery gates: mission → playbook (N-of-M) → RR → EV floor → contract freshness
  → telegram → tracker (telegram_sent=True)
```

| Layer | Decides | Does NOT decide |
|-------|---------|-----------------|
| **Fusion** (`detect/*`) | Side, `gate_open`, `fusion_score`, CUSUM `phase` | Playbook, RR, TG |
| **Playbook** (`analysis/playbook_eval.py`) | N-of-M checklist pass (default delivery authority when `HUNT_PWIN_GATE=0`) | Side selection |
| **Mission** (`gate/_mission.py`) | Pre-* phase only; blocks `mid` / legacy mid-leg | Magnitude threshold |
| **Tracker** (`track/tracker.py`) | Follow-ups after TG | Pre-TG funnel (see `setup_candidates.jsonl`) |

**Phase vocabulary:** live ticks write fusion phases (`pre_pump`, `pre_dump`, `mid`, `neutral`) via CUSUM. Mission/sniper gates accept fusion + legacy aliases through `gate/_phase_compat.py`. The removed 10-state FSM (`exhaustion_at_high`, `dump_active`, …) appears only in closed tracker rows and `/signal` copy — not as the tick writer.

**`fusion_score` vs gate:** gate uses vol-adjusted magnitude quantile (`q_gate`); `fusion_score` is a 0–100 strength index for ranking/telemetry — correlated but not identical. See [FUSION_PARAMS.md](FUSION_PARAMS.md).

Legacy numbered stack below kept for historical reference — superseded by the chain above.

1. Data readiness  
2. **Mission lock** — pre-* lifecycle only for watch TG  
3. **Playbook checks** (shared checklist → fusion rank + `_decl_check_playbook`)  
4. Lifecycle FSM — `short_entry_ok` / `long_entry_ok`  
5. Structure — `confirm_hard` (1m dump / 5m long)  
6. MTF confluence  
7. RR + levels (ATR-relative, no mid-leg continuation ladders on watch)  
8. Contract  
9. EV floor + P(win) shadow (P(win) blocks only when `HUNT_PWIN_GATE=1`)

---

## 4. Defaults

| Env | Default | Meaning |
|-----|---------|---------|
| `HUNT_WIDE_MODE` | `0` | No wide hunter / continuation bypass |
| `HUNT_SNIPER_MODE` | `1` | Pre-dump/pre-pump phases only for live TG |
| `HUNT_LONG_TG` | `1` (ops) | Pre-pump long TG when mission + playbook pass; independent of `HUNT_WIDE_MODE` |
| `HUNT_PWIN_GATE` | `0` | min_p_win shadow-only; playbook + RR gates delivery |
| `HUNT_LEGACY_SCANNER` | `0` | Rank-budget watchlist (deprecated rollback) |
| `HUNT_LEGACY_FUEL` | `0` | Legacy fuel merge off — playbook path only |

---

## 5. Parameter provenance (§2.9)

| Tier | Examples | Change rule |
|------|----------|-------------|
| **A — product policy** | mission phases, confirm TF, no autotrade | Only on product change |
| **B — named standards** | Wilder RSI/ATR, VP 70% VA, OI Adler bands | Cite source; rare tune |
| **C — incident post-mortem** | BEAT trail MFE, JCT exhaustion | Ledger tag + review at N≥50 |
| **D — provisional** | legacy fuel weights | Deprecated on default path; ledger calibrates |

---

## 6. Anti-patterns (explicitly rejected)

- Shorting `dump_active` or longing `impulse_initiating` on watch TG.  
- Loosening gates because «no signals».  
- Second CCXT client for `/signal` on watched symbols (prefer store read).  
- TP/SL repair to pass contract.

---

## 7. Success metrics

**Primary (outcome quality):**

- Hold-to-target SL rate ≤30%, TP1+ ≥50% (n≥30) on delivered signals
- `replay_fusion`: precision / coverage / lead_time stability across `q_gate` sweep (0.88–0.96)
- Authority audit: zero `authority_violation` rows in `hunt_outcome_ledger.jsonl` (`python -m hunt_core._dev.authority_audit`)

**Operational (secondary):**

- Delivery rate on **pre-*** phases only  
- Blocker entropy: `mission_mid_dump` / `squeeze_blocks_predump_short`  
- `/signal` p95 < 5s for watched symbols  
- Outcome ledger: `fusion_gate_open`, `playbook_pass_ok`, `mission_pass`, `phase_fusion` at every deliver/block boundary
