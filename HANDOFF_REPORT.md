# HANDOFF_REPORT — 2026-06-05

## Session summary

- Wave tests: **154 passed** (`test_wave_f9/f10_*`, `test_wave_i_calibration`)
- `compileall` + `validate_config` — OK
- Bot runtime at handoff: started via agent (`python main.py run` after smoke clean)

## SL / delivery fixes (T-block + N-block)

| Area | Status | Key change |
|------|--------|------------|
| T-1 confirmed bar | ✅ | `confirmed_pattern_frame()` — 21/21 strategies on closed bar (`row[-2]`) |
| T-2 TP at expiry | ✅ | `_expiry_event_type()` + `outcomes.py` G1 remap `expired_active` → `tp1_hit` |
| T-3 long/short HTF gate | ✅ | `_htf_direction_allows()` symmetric in `_analyzer_gates.py` |
| T-4/N-1 base_score | ✅ | Catalog + `bot/strategies/*.py` + **`config.toml`** synced; dead setups ≥0.60, whale_walls **0.62** |
| T-6 short+downtrend+ATR | ✅ | Hard reject `short_downtrend_high_atr` in `filters.py` |
| T-7 whale_walls cooldown | ✅ | `delivery.setup_interval_minutes = { whale_walls = 20 }` |
| T-9 long/short telemetry | ✅ | `TelemetryManager.session_direction_snapshot()` → `health.jsonl` |
| N-2 score floor | ✅ | `_build_signal` floor **0.38 → 0.63** (`setups/__init__.py`, `_roadmap.py`) |
| N-4 reversal min_rr | ✅ | `min_rr = 2.5` for 6 reversal setups in `config.toml` |
| Legacy DROP (T-8) | ✅ | Migration v9 applied; dashboard reads `active_signals` only |

## Config highlights (`config.toml`)

- `filters.min_score = 0.65`
- `filters.max_atr_pct = 1.8`
- `delivery.action_min_score = 0.65`
- `delivery.setup_interval_minutes.whale_walls = 20`
- All `[bot.filters.setups]` `base_score` ≥ 0.60 (exceptions at 0.62 per strategy)
- Reversal `min_rr = 2.5`: `wyckoff_spring`, `turtle_soup`, `rsi_divergence_bottom`, `wick_trap_reversal`, `liquidity_sweep`, `stop_hunt_detection`

## Proxy / network

- Primary egress: `socks5://206.123.156.224:6290` in `[bot.network]`
- Agent owns proxy discovery / failover — not operator task

## What to monitor next live session

1. `health.jsonl` → `signals_long_session` / `signals_short_session` (post T-3 long-gate fix)
2. Reject reasons: `setup_interval_cooldown_active`, `short_downtrend_high_atr`, `min_score`
3. Outcome remap: no new `expired_active` when `tp1_hit_at` set
4. Strategy hit rate per setup after N-1/N-2 (expect fewer CPU rejects, more viable candidates)

## Files to read before changes

1. `CLAUDE.md`
2. `docs/DEFINITION_OF_DONE.md`
3. `bot/runtime/delivery_orchestrator.py`
4. `bot/delivery/filters.py`
5. `bot/persistence/_tracking_review.py`
6. `config.toml` — **runtime overrides code defaults**

## Invariants (unchanged)

1. No auto-trade / no private Binance endpoints
2. Delivery: `validate_signal_contract` → hard confluence gate → `deliver`
3. Strategies only under `bot/strategies/`
4. No new test files without explicit request
5. Backlog IDs only from `docs/DEFINITION_OF_DONE.md`

## Verify commands

```bash
python -m compileall -q bot
python scripts/validate_config.py --config config.toml
pytest tests/test_wave_f9_agent_*.py tests/test_wave_f10_agent_*.py tests/test_wave_i_calibration.py -q
python scripts/clean_session_data.py --mode smoke --config config.toml
```
