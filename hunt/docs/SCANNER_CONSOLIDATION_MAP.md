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
