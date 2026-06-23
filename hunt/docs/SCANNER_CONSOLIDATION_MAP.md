# Scanner consolidation map

Policy: **quarantine-not-delete** until Phase 8 OOS gate.

| Block | P/I/D | Target | Status |
|-------|-------|--------|--------|
| `detect/factors` (6 core) | production | fusion | production |
| `analysis/expansion_engine` | port | quarantine factors | quarantine |
| `maps/forecast` | input | playbook + levels | production input |
| `analysis/manipulation_fusion` | required | arbiter confluence | production |
| `setups/catalog` | lab | EV promotion | lab lane |
| `expansion_alerts` | advisory | lab TG | lab |
| Tier-1 lake cols | data | new factors | quarantine |
| `market_maker_trap` | port | fusion factor | quarantine |
| `whale_activity` | port | fusion factor | quarantine |

Factor registry: `scanner/detect/factor_registry.json`  
Promotion gate: `python -m hunt_core._dev.factor_promotion_gate`

Hard delete allowed only after Phase 8 OOS + multiple-comparison pass.

## `maps/forecast` — why it survives (2026-06-23)

**Decision:** **retained** as production **input**, not a standalone TG authority.

| Question | Answer |
|----------|--------|
| Why not delete with old scan stack? | Playbook checks and `levels/` TP bands still consume forward ATR/move envelopes from `maps/forecast.py`. |
| Who owns the trade decision? | Fusion + delivery gates — forecast does not emit `confirmed` or bypass arbiter. |
| Operator TZ “remove forecast”? | Interpreted as **remove forecast-as-authority** (done); bands as **geometry input** remain until levels/playbook read ATR envelopes from a single `shared/primitives` helper. |
| Removal criteria | Phase 8 OOS shows playbook+levels parity after migrating band math off `maps/forecast`; then quarantine → delete per table above. |
