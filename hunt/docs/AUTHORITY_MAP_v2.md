# AUTHORITY_MAP v2 — post-remediation (2026-06-21)

Canonical numbering: **Module 1 = Deep** · **Module 2 = Scanner**

Target architecture after audit remediation. See [MIGRATION_PLAN.md](MIGRATION_PLAN.md).

## Module 2 — Scanner / fusion production lane

```
CCXT facts → fusion (detect/*) → manipulation_fusion/playbook → mission/RR/EV gates
  → evaluate_confirm_authorities (arbiter)
  → run_gate_pipeline
  → production Telegram (TELEGRAM_CHAT_ID)
```

**Single arbiter** (`delivery/arbiter.py`): requires `setup.confirmed` + playbook N-of-M + mission pass + fusion snapshot.

**Unified cooldown**: `xchan:{SYMBOL}:{direction}` persisted in `data/hunt_delivery_state.json`, merged with tick `state` on load/save. Spans fusion advisory stages (`unified:*`) within Module 2.

## Module 2 — Lab lane (E1)

```
Catalog EV / expansion advisory / EV bootstrap
  → route_delivery_lane == "lab"
  → contract check only (no production arbiter)
  → TELEGRAM_LAB_CHAT_ID
  → hunt_lab_outcome_ledger.jsonl
```

Expansion pinned alerts (`runtime/expansion_alerts.py`) stamp `lab_alert` and use `send_lane_html`.

## Module 1 — Deep (unchanged independence)

Verdict V2 + deep pinned loop keep **separate** cooldown and TG policy. Cross-module messages on the same symbol are expected (OPEN_DECISIONS J).

## Detection producers — roles after A1

| Producer | Role |
|----------|------|
| `detect/fusion` | **Authority** — side, phase, `confirmed`, `fusion_score` |
| `analysis/manipulation_fusion` + playbook | **Required confluence** input to arbiter |
| `maps/forecast` | Input bands for levels/playbook — no standalone prod TG |
| `setups/catalog` | Feature detectors; EV promotion **lab only** |
| `analysis/expansion_engine` | Input scores + **lab TG** until counterfactual calibration |

## Delivery channels

| Channel | Lane | Cooldown |
|---------|------|----------|
| Fusion confirm | Production | Arbiter + unified + xchan |
| Advisory (squeeze, liq) | Production | unified stages + xchan |
| Expansion pinned | **Lab** | expansion alert state + xchan exempt |
| Deep / Verdict | Module 1 | Own policies |

## Invariant audit

`python -m hunt_core._dev.authority_audit` — production ledger rows must have zero `authority_violation` and no `delivered` with false playbook/mission/fusion flags.
