# Hunt methodology (canonical summary)

**Module 1 Deep:** Verdict V2 scenario engine on pinned anchors — structure, VP, derivatives, cross consensus. Independent cooldown/TG.

**Module 2 Scanner:** Closed-bar fusion (6 factors) + playbook N-of-M + gate stack → production TG. Lab lane for catalog EV and expansion advisory.

**Shared:** CCXT facts, prepare pipeline, mathlib, primitives, shadow ledger.

## Delivery invariant (Scanner production)

`confirmed` + playbook pass + mission pass + gate pipeline + arbiter → contract → Telegram

## Strategic gates (Phase 1.5, shadow until Phase 8)

- Move-significance (`scanner/gate/_move.py`)
- Tradability / fill proxy (`scanner/gate/_tradability.py`)
- SignalHorizon → TTL + cooldown (`domain/signal_horizon.py`)
- ATR-based entry zones (catalog + levels)
- COLD/WARM/HOT factor confidence (`detect/calibrate.symbol_state_tier`)

## Validation discipline

- Quarantine-not-delete for new factors
- `replay_fusion --walk-forward 0.3` mandatory
- Shadow reject log: `data/hunt_shadow_rejects.jsonl`
- Phase 8 OOS gate for hard delete and `*_probability` labels

See also: `MODULE_OWNERSHIP_MAP.md`, `SCANNER_CONSOLIDATION_MAP.md`, `AUTHORITY_MAP_v2.md`
