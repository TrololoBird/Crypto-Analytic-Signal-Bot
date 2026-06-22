# TARGET_ARCHITECTURE — two independent modules

Date: 2026-06-21. Authority: operator brief §1. Canonical numbering: **Module 1 = Deep
Analysis, Module 2 = Pre-pump/Pre-dump Scanner** (current code uses the reverse — migrate).

Product framing: outputs are **future signals** — a conditional plan (entry zone +
activation trigger: limit-in-zone, or market-on-entry+confirm), not "act now".

---

## Layering — shared raw facts, independent interpreters

```
                 ┌─────────────────────────────────────────────┐
                 │ CCXT market plane (REST + Pro WS, public)    │  SHARED
                 │ market/client.py, market/streams.py          │
                 └───────────────────┬─────────────────────────┘
                                     │
         ┌───────────────────────────┴───────────────────────────┐
         │ Layer 0A — raw deterministic facts (strategy-free)     │  SHARED, computed ONCE
         │ price, volume, oi, funding, delta, atr, poc, hvn, lvn  │
         │ features/prepare*.py, features/volume_profile.py, maps │
         └───────────────────────────┬───────────────────────────┘
                                     │
         ┌───────────────────────────┴───────────────────────────┐
         │ Layer 0B — parameterized facts (deterministic after    │  SHARED registry,
         │ param fixed). Name carries the param:                  │  per-module ORDERS
         │ bos_htf, choch_15m, btc_beta_90, compression_20        │
         │ features/structure.py, features/microstructure.py      │
         └───────────────┬───────────────────────┬───────────────┘
                         │                       │
        ┌────────────────┴──────────┐  ┌─────────┴────────────────────┐
        │ MODULE 1 — DEEP ANALYSIS  │  │ MODULE 2 — PRE-MOVE SCANNER   │
        │ (Full data tier)          │  │ (Lite scan → Full on survivors)│
        │ analysis/deep + verdict_v2│  │ ONE canonical detection core  │
        │ own strategies/filters    │  │ own pre-pump/pre-dump method  │
        │ LONG/SHORT/WAIT + plan    │  │ ranked candidates + plan      │
        └────────────┬──────────────┘  └─────────────┬─────────────────┘
                     │                               │
        ┌────────────┴───────────┐      ┌────────────┴────────────┐
        │ Arbiter 1 (per symbol) │      │ Arbiter 2 (per symbol)  │
        │ 1 cooldown · precedence│      │ 1 cooldown · precedence │
        └────────────┬───────────┘      └────────────┬────────────┘
                     └───────────────┬───────────────┘
                          ┌──────────┴──────────┐
                          │ Telegram (manual)   │  closed-bar confirm
                          │ + Outcome Ledger    │  geometry on EVERY row
                          └─────────────────────┘
```

### Independence rules (from brief §1)
- Zero shared decision logic; no cross-import between strategy layers; neither module calls
  the other.
- Shared only: (a) CCXT plane, (b) Layer 0A raw facts, (c) outcome-ledger infra.
- Each raw fact computed once; each module reads its slice.
- Layer 0B facts deterministic only after the param is fixed; the fact name encodes the
  param (`btc_beta_90`, `compression_20`). Each module may order 0B facts at its own
  sensitivity.
- **Score ≠ probability** until calibration: all outputs are `*_score`; `*_probability`
  appears only after Outcome Ledger + calibration. Already honored
  (`detect/fusion.py:63`); enforce across expansion/catalog too.

---

## Module 1 — Deep Analysis
- **Input:** specific coins — pinned + operator-sent (chat/command).
- **Owner:** `analysis/deep/` + `analysis/deep/verdict_v2/` (consolidate `deep_signal.py`,
  `verdicts.py`, `forecast_panel.py`, `fusion_panel.py`).
- **Vocabulary:** does NOT use pre-pump/pre-dump terms.
- **Output:** LONG/SHORT/WAIT-until-trigger + plan (entry zone, SL, TP1/2/3, invalidation,
  activation condition, horizon, fragility).
- **Data tier:** Full (`HUNT_FULL_PREPARE=1`, microstructure, deep maps).
- **Delivery:** Arbiter 1 → one cooldown over T4+T5 (deep pinned change + verdict queue).

## Module 2 — Pre-move Scanner
- **Input:** universe → scanner filter (`data/scanner.py`, `data/universe.py`).
- **Owner:** ONE canonical detection core (choice in OPEN_DECISIONS §A). Other current
  producers become **feature/score inputs** to that core or are **deleted as dupes**.
- **Output:** ranked pre-pump/pre-dump candidates + a conditional signal each.
- **Data tier:** Lite cheap scan over hundreds → Full deepen on survivors. Smaller set than
  Module 1, overlapping.
- **Delivery:** Arbiter 2 → one cooldown spanning T1+T2+T3+T6+T7, explicit precedence.

---

## Facts × timeframe (MTF) matrix (target)
Layer 0A/0B facts are stored per `(symbol, tf)`; modules request the TFs they need.
Allowed TF set must be **fetched-or-rejected** (fix §3.4: drop `3m` or implement it).

| Fact class | Example facts | TFs (typical) |
|------------|---------------|---------------|
| 0A raw | price, volume, oi, funding, delta, atr, poc/hvn/lvn | 1m,5m,15m,1h,4h,1d (1w BTC/ETH/XAU/XAG) |
| 0B param | bos_htf, choch_15m, compression_20, btc_beta_90 | per module order |

## Lite vs Full data tiers
- **Lite (Module 2 scan):** kline + OI + funding + LS-ratio + 24h ticker; no deep book, no
  microstructure. Drives the cheap universe pass.
- **Full (Module 1 + Module 2 survivors):** + L2 book depth, microstructure CVD/delta,
  liquidation map, multi-period volume profile.
- Tier is already a runtime concept (`snapshot_tier`, fast vs full,
  `data_readiness.py:211`); formalize as the module-data contract.

## One arbiter per module
- Subsumes the three authorities (fusion/playbook/mission) into the module's single
  decision; `authority_violation` becomes structurally impossible (one gate, one writer).
- One cooldown per module spanning all its sources (replaces the 6 private cooldowns).
- Explicit precedence list when multiple in-module detectors fire on one symbol.

## Invariants carried forward (unchanged)
No auto-trading; no private Binance auth; public CCXT only; manual Telegram; closed-bar
confirm; geometry persisted on every ledger row (deliver + block) for unbiased calibration.
