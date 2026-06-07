# Project roadmap & status (v9)

> **Last updated:** 2026-06-06  
> **v1 Definition of Done:** [DEFINITION_OF_DONE.md](DEFINITION_OF_DONE.md) ← единственный backlog  
> Agent tokens: [AGENT_TOKEN_POLICY.md](AGENT_TOKEN_POLICY.md)  
> Refactor matrix: [REFACTOR_PLAN.md](REFACTOR_PLAN.md)

---

## Executive summary

| Area | State |
|------|--------|
| **v1 product (signal factory)** | ✅ Ready for production ops — see [DEFINITION_OF_DONE.md](DEFINITION_OF_DONE.md) |
| **v9 package layout** | Done — single `bot/strategies/`, `market/`, `features/`, `runtime/`, `delivery/`, `persistence/`, `engine/` |
| **Waves E1–E8** | Done — delivery gates, WS/order-flow, dashboard, network, persistence v5 |
| **Wave F9** (agents K–U, 10 modules) | Done — domain strict config, runtime telemetry, SMC/OB, regime bear carve-out, universe, diagnostics, journal, delivery tiers, telemetry KPI, ops CLI |
| **Wave F10** (5 tasks × 10 modules) | Done — family_gates, weighted confluence bridge, calibration pipeline, global regime, shortlist prescore, migrations v6 |
| **Wave F11** (ops bridge) | Partial — live_watch ↔ matrix/calibration, rollup report, `config.toml.example` strict sync |
| **Live validation** | 6h supervised sessions completed; current session tooling: `live_supervised_session`, `live_watch_rollup_report` |
| **F12 de-bloat** | Partial — pipeline/ws_transport/memory_schema done; memory/tracking/ws → v1.1 |
| **LLM APIs in bot** | Deferred OPT-2 |

---

## Completed work (by wave)

### E1–E8 (foundation)

- Strategy catalog wiring, hard confluence gate (3-of-5), bear regime policy (`resolve_bear_regime`)
- Reversal `reversal_min_confirmations=2`, portfolio caps, freshness guards
- Order-flow WS ingest, subscription planner, data readiness
- Dashboard tabs (funnel, runtime, shortlist, tracking, outcomes), operator context
- Network proxy bootstrap + discovery scripts
- Persistence: journal normalization, diary fixes, outcomes classification, migration v5 index
- **Tests:** `tests/test_wave_e1_*` … `tests/test_wave_e8_agent_{a..j}.py`

### F9 (10 modules — K through U)

| Agent | Module | Delivered |
|-------|--------|-----------|
| K | `bot/domain/` | Strict TOML models, catalog param keys, RU labels, runtime call-path guard |
| L | `bot/runtime/` | Async network ready, lane-skip aggregation, `min_score` floors, cycle timeout telemetry |
| M | `bot/setups/` + `order_block` | FVG via `smc.fvg_candidates`, OB 15m, SMC spec tier |
| N | `bot/regime/` + orchestrator | `global_market_regime`, BTC phase penalty, HTF reversal carve-out in bear |
| O | `bot/market/universe.py` | Basis warm, outcome derank, REST weight budget, wash/spread gates |
| Q | diagnostics + `live_audit` | Routing skips, quality monitor, `shortlist_not_routed` semantics |
| R | journal / diary / outcomes | `normalize_tracking_event`, migration **v6** diary.symbol, `queries/outcomes` |
| S | `bot/delivery/` | HTML validate, RR tiers, WATCH/ACTION, weighted confluence **bridge** (config flag) |
| T | telemetry + `live.py` | Delivered KPI = sent/logged only, `run_id` on JSONL, emergency counts |
| U | CLI / Makefile / supervisor | `--config`, `calibration_pipeline.py`, `nightly-calibration`, supervisor |

- **Tests:** `tests/test_wave_f9_agent_{k,l,m,n,o,q,r,s,t,u}.py` (88+ cases)

### F10 (5 tasks × 10 modules)

- **L:** `family_gates.py`, intra-candle fast lane, merge conflicts, delivery semaphore path
- **M:** `is_clean_fvg`, `sweep_tolerance`, `build_smc_trade_plan`, BOS spec alignment
- **N:** `regime_frame_4h`, funding median, btc_phase penalty integration
- **O:** wash-volume prescore, spread gate, SL decay derank
- **Q–U:** routing audit, repo-primary journal, watch escalation, funnel API, `calibration_pipeline`, `errors.py`
- **Tests:** `tests/test_wave_f10_agent_{k..u}.py` — **188 passed** with F9 + E8 fixes (2026-06-04)

### F11 (ops — partial)

- `bot/diagnostics/live_watch.py` — resolve `data/live_watch/{run_id}` vs `telemetry/runs`
- `scripts/strategy_shortlist_matrix.py` — `--run-id`, `--live-watch-dir`; fix `status` field in `analyze_telemetry`
- `scripts/live_watch_rollup_report.py`, `make live-watch-report`
- `scripts/calibration_pipeline.py` — `--run-id` slice
- `config.toml.example` — duplicate webhook section removed; validates clean
- `tests/test_wave_i_calibration.py` (replaces removed `test_wave_f11_live_watch_bridge.py`)

### Live ops

| Session | Path | Result |
|---------|------|--------|
| 6h (2026-06-04) | `data/live_watch/20260604T014627Z/` | ~11 440 cycles, **124** delivered, exit 0 |
| Rollup | `data/live_watch/rollup_20260604T074703Z.json` | Consolidated session stats |
| 6h (in progress) | `data/live_watch/20260604T111149Z/` | Started via `live_supervised_session` |

---

## Remaining work (prioritized)

### P0 — Production signal quality (after W1–W3 + harvest)

1. **Enable / calibrate `use_weighted_confluence`** — **last wave (W4)** after architecture + strategy changes stabilize.
2. **Post-6h calibration loop:** `BOT_ALLOW_CALIBRATION=1 calibration_pipeline --run-id <id>` when REST reachable.
3. **Proxy / network:** keep `[bot.network]` via `discover_binance_proxies.py` — required in RU/geo-blocked regions.

### P1 — Structural de-bloat (F12)

| Target | LOC (approx) | Action |
|--------|-------------|--------|
| `bot/persistence/repository/memory.py` | ~2073 | Split query groups → `persistence/queries/` (R9 started) |
| `bot/runtime/analyzer/pipeline.py` | ~1554 | Extract cycle dispatch / conflict merge (L4/L5 full) |
| `bot/runtime/bot.py` | ~1015 | Thin orchestration only |
| `bot/market/ws.py` | ~1937 | Continue ws_connection split |
| `bot/persistence/tracking.py` | ~1935 | Align with tracking channel lifecycle tests |

### P2 — Analyzer & backtest

- **Phase 3:** slim `symbol_analyzer` entry — deferred; track via `project_health_audit.py`
- **U1:** backtest vs live feature parity (`bot/backtest/` vs `bot/features/`) — Wilder ATR/RSI, no `shift(-N)` on live path
- **order_block.py:** adopt `is_clean_fvg` / `sweep_tolerance` where duplicate logic remains

### P3 — Observability

- Parse `strategy_decisions` from live_watch `bot_stdout.log` when telemetry JSONL missing (F11 extension)
- Dashboard: ensure `/api/health` during supervised runs (HTTP servers lifecycle)
- Prometheus funnel/reconcile APIs (F10-T) — wire to Grafana if needed

### P4 — Strategy calibration (ops waves)

- Per-strategy zero-hit triage (`zero-hit-strategy-triage` skill)
- Nightly: `make nightly-calibration` / `strategy_shortlist_matrix --live-shortlist`
- Outcomes-driven derank tuning from SQLite (`outcome_derank`)

---

## Verification commands

```bash
source .venv/bin/activate
python -m compileall -q bot
python scripts/validate_config.py --config config.toml
PYTEST_LIVE=1 pytest tests/live/ -v
# When Binance REST reachable:
PYTEST_LIVE=1 pytest tests/live/ -v
python scripts/live_check_pipeline.py --symbols BTCUSDT --limit 1
```

## Live session commands

```bash
python -m scripts.live_supervised_session --hours 6 --minutes 360 --snapshot-interval 60 --takeover
python scripts/live_watch_rollup_report.py
python scripts/calibration_pipeline.py --run-id <RUN_ID>
make live-watch-report
```

---

## Notes for next agent session

- Cursor **Task subagents** were rate-limited (2026-06-04); rerun F12 splits when Auto/limits allow.
- Do not commit `config.toml`, `.env`, `data/`, `logs/`.
- Delivery path invariant: `validate_signal_contract` → `hard_confluence_gate` → `deliver` — never bypass.
