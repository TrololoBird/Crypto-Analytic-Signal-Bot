# Hunt reference gap analysis

External eligible OSS ↔ Hunt (`hunt/`, `hunt_core/`, `hunt_watch/`) only.

**Last audit:** 2026-06-13 deep audit + fix wave.

---

## Summary (honest)

| Track | Status | Notes |
|-------|--------|-------|
| Product effectiveness | **~40%** | dump_active short edge proven (n=86); live n=31; long=0 |
| P0 delivery code | **~90%** | report=dispatch parity, TP1 demotion, phase imminent fixed |
| P1 detect/gate | **~85%** | closed-bar fuel partial; sticky invalidation fixed |
| Architecture | **~35%** | hunt_core 34k LOC; collect/cycle monoliths; dual stack |
| Measurement | **~70%** | gate_edge + feature report; logic_verify expanded |
| R3 cutover | **~75%** | hunt_watch shims remain |

**Not 98% done.** See [HUNT_TRUTH_AUDIT.md](HUNT_TRUTH_AUDIT.md).

---

## Fixed this wave (2026-06-13)

| Issue | Fix |
|-------|-----|
| `*_imminent` unreachable | phase_dump/long order swap |
| RSI osc_band false rescale | removed heuristic |
| Ichimoku senkou shift | engine.structure.ichimoku_lines |
| Report ≠ dispatch gates | primary_block_for_report → evaluate_delivery |
| TP1 progress hard block | freshness split; demote to ARMED |
| Confluence div relax dead | div bypass when min_struct_eff=1 |
| Pivot support look-ahead | closed_bar required |
| Sticky suppresses invalidation | honor raw invalidate_short |
| PP live 15m true break | strip pp_*_true when closed=False |
| Advisory TG bypass | HUNT_ADVISORY_TG=0 default |
| bias=wait dump_active TG | cycle block |
| bb_upper/bb_lower, obv_rising | prepare_frame exports |
| Kinematic funnel tag | kinematic not wash |

---

## Open (architecture Track 2)

| Gap | Priority |
|-----|----------|
| collect.py ~2.1k LOC split | P1 |
| cycle.py ~1.8k LOC split | P1 |
| scoring.py split confirm/fuel/phase | P2 |
| hunt_core LOC 34k → 8k milestone | P2 |
| hunt_watch physical freeze | P2 |
| Fuel HTF closed-bar full audit | P1 |
| Sniper vs fade-at-top exception | P2 (wide mode default off sniper) |

---

## Verification

```bash
PYTHONPATH=hunt .venv/bin/python3 hunt/scripts/verify_logic.py
PYTHONPATH=hunt .venv/bin/python3 hunt/scripts/gate_edge.py --direction both
PYTHONPATH=hunt .venv/bin/python3 hunt/scripts/gate_edge.py --report --min-n 30
PYTHONPATH=hunt .venv/bin/python3 hunt/scripts/check_core_budget.py
PYTHONPATH=hunt .venv/bin/python3 hunt/scripts/critical_audit.py --symbols BTCUSDT,ETHUSDT
```
