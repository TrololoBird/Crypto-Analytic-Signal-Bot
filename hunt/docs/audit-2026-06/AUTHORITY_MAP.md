# AUTHORITY_MAP — actual detection→delivery→precedence→cooldown graph

Date: 2026-06-21. Built from code, not docs. Every edge has a `file:line`.
All confidence `[CONFIRMED]` from reading unless tagged.

## Detection sources (who decides a side / pre-move state)

| # | Source | Output | Entry point |
|---|--------|--------|-------------|
| D1 | Fusion engine | `pre_pump`/`pre_dump` + side + `fusion_score` + `confirmed` | `detect/live.py:15` → `detect/fusion.py:83` `fuse` → `detect/phase.py:42` `assess_phase` → `detect/result.py:95` `build_detection` → `detect/delivery_setup.py:102` `build_delivery_setup` |
| D2 | Expansion engine | PRE-PUMP/PRE-DUMP probability + archetype (24 blocks) | `analysis/expansion_engine/expansion/orchestrator.py:84` `score_base_blocks` → `…/forecast/engine.py`, `…/ranking/scan.py` |
| D3 | Setup catalog | CEX burst setups + EV | `setups/catalog.py` (cex_* `config.defaults.toml:211-216`), `setups/detectors.py` |
| D4 | Maps forecast | `prepump_long`/`predump_short`/`ignition_long` bands | `maps/forecast.py:201,229,272` `build_*_forecast` |
| D5 | Manipulation fusion / playbook | `predump_short`/`coil_long`/`ignition_long` checklist | `analysis/manipulation_fusion.py:9`, `analysis/playbook_eval.py:33-35`, `analysis/playbook_checks.py` |
| D6 | Verdict V2 (deep) | LONG/SHORT/WAIT scenario verdict | `analysis/deep/verdict_v2/`, `analysis/deep/verdicts.py` |

## Delivery channels (who can send Telegram) + cooldown boundary

| # | Channel | Trigger | Cooldown (independent) | Code |
|---|---------|---------|------------------------|------|
| T1 | Fusion advisory→confirm | D1 `confirmed`/`intrabar_confirmed` + geometry contract | `unified:{sym}:{dir}:{stage}` 45 min over `early/dump_hunt/squeeze/confirm`; **state is a caller dict, plane-local** | `deliver/dispatch.py:25-86,189-227`; gate evaluated in `runtime/cycle/_delivery.py`, `_cycle_tick.py` |
| T2 | Expansion pinned alerts | D2 quality/trigger ≥ thresholds, on-change | `tg_cooldown_min=45` (own state) | `runtime/expansion_alerts.py`; `config.defaults.toml:101-106` |
| T3 | Expansion universe digest | D2 batched top-N | `tg_universe_interval_s=900` (own timer) | `runtime/expansion_universe_scan.py:142-200`; `config.defaults.toml:108-111` |
| T4 | Deep pinned change | D6 verdict change | `tg_on_change` + `tg_stale_hours=4` (own) | `runtime/deep_assembly.py:354-423`; `config.defaults.toml:75-78` |
| T5 | Verdict V2 signal queue | D6 queue top-N | `signal_queue_*` batch (own) | `config.defaults.toml:124-128`; `runtime/query_service.py`, `runtime/signals_report.py` |
| T6 | Watch ignition / liq-burst advisory | D1/scan advisory stages | `telegram_cooldown_min=45`, `followup_cooldown_min=5` (own) | `runtime/cycle/_cycle_advisory.py:32-190`, `_cycle_loop.py:530-545`; `config.defaults.toml:18-22` |
| T7 | Catalog `/signals` + EV promotion | D3 EV bootstrap | `HUNT_EV_BOOTSTRAP` default on; promotion path | `setups/catalog.py:164-187`; `runtime/signals_report.py` |

## Precedence

`[CONFIRMED]` **There is no cross-channel precedence arbiter.** Each loop in
`runtime/cycle/_cycle_loop.py` independently spawns `deep_pinned_loop` (T4),
`expansion_universe_scan_loop` (T3), advisory (T6), and the fusion confirm path (T1) as
concurrent tasks (`_cycle_loop.py:278-341,530-545,744`). Within T1 only, stage ranking
exists (`dispatch.py:30-31 _stage_rank`, order `early<dump_hunt<squeeze<confirm`). Across
channels, the only de-dup is each channel's private cooldown.

## Cooldown boundary summary

- T1 cooldown spans the **fusion advisory + confirm stages** for one `sym+dir` — but the
  `state` dict is supplied by the caller (`dispatch.unified_cooldown_ok(state, …)`), so it
  is scoped to whatever runtime object owns it; it does **not** include T2–T7.
- T2, T3, T4, T6 each carry a **separate** 45-min / interval cooldown.
- **Consequence:** the same symbol+direction can produce a fusion confirm (T1), an
  expansion pinned alert (T2), a universe-digest line (T3), and a watch advisory (T6)
  inside the same 45-min window — four messages, four ledgers, no shared suppression.

## Authority invariant (confirm boundary)

Recorded per delivery in `track/outcome_ledger.py:33-73`:

```
fusion_gate_open = setup['confirmed']                       # D1 plane
playbook_pass_ok = mf.pass_count >= mf.required_n           # D5 plane
mission_pass     = no "mission_" blocker                    # gate plane
authority_violation = delivered AND (not fusion_gate_open
                       OR playbook_pass is False
                       OR mission_pass is False)
```

Audited by `_dev/authority_audit.py:36-77`. **The delivery decision
(`dispatch.evaluate_delivery`, `dispatch.py:216-221`) only enforces `fusion_gate_open` +
geometry** — playbook/mission can disagree and a row still ships, which is the exact
condition the auditor was built to catch. Target state (§1 of brief): **one arbiter per
module** that subsumes all three authorities for that module, with explicit precedence and
one cooldown spanning every source of the module.
