# ADR: G2 H-B Review (2026-06-13)

## Status

Accepted — **interim H-A core + H-C measurement shell** until per-slice n≥30 PASS.

## Context

Gate edge refresh (2026-06-13): short confirmed SL **26%** (n=200), best slice **dump_active** SL **19.8%** TP1+ **67.4%** (n=86). Live tracker thesis_success **71%** but tp_hit **13%**. hunt_core **34k LOC** vs 8k budget.

G2 chose H-B Wide Hunter despite data favoring short fade dump_active only.

## Decision

1. **Live confirm TG:** short `dump_active` path primary; other phases MEASURE before promote.
2. **Long TG:** remain off (`edge_policy`) until gate_edge long n≥30 PASS.
3. **Advisory TG** (squeeze, ignition, dump_hunt): **off by default** (`HUNT_ADVISORY_TG=0`); log-only.
4. **Product invariant:** block confirm TG when `dump_active` short + `recommended_bias=wait`.
5. **North Star:** `bt_outcome` hold-to-target only — not thesis_success.
6. **Report parity:** `/signals` re-alert uses `evaluate_delivery()` stack.

## Consequences

- Lower TG volume (correct — funnel was 1.5% for a reason).
- Code fixes for measurement integrity (phase FSM, RSI, Ichimoku, delivery parity).
- Architecture de-bloat deferred to milestone 34k→20k→8k LOC.

## References

- [HUNT_TRUTH_AUDIT.md](HUNT_TRUTH_AUDIT.md)
- [HUNT_PRODUCT_DEFINITION.md](HUNT_PRODUCT_DEFINITION.md)
