# Claude Code Quick Reference

Updated: 2026-06-04 · Full: `CLAUDE.md` · Hot paths: `AGENT_QUICK_START.md`

## Product

Public Binance USDⓈ-M → 38 detectors → contract + 3-of-5 confluence → Telegram. **No** auto-trade or private API.

## Entry points

| What | Where |
|------|-------|
| CLI | `main.py` → `bot/cli.py` |
| Bot | `bot/runtime/bot.py` |
| Config | `bot/domain/config.py` |
| Strategies | `bot/strategies/__init__.py` |

## Delivery invariant

`validate_signal_contract` → `hard_confluence_gate` (3/5) → `delivery.deliver`

Key: `bot/delivery/contract.py`, `bot/runtime/delivery_orchestrator.py`, `bot/delivery/deliver.py`

## Data flow

WS/REST → shortlist/universe → prepare_frame → engine/strategies → symbol_analyzer → orchestrator → Telegram

Hot: `bot/market/ws.py`, `bot/market/rest_impl.py`, `bot/features/prepare_frame.py`, `bot/runtime/symbol_analyzer.py`

## Commands

```bash
make check && make validate-config
python scripts/clean_session_data.py --mode smoke --config config.toml
python main.py run && make live-smoke
```

Calibration: `BOT_ALLOW_CALIBRATION=1 python scripts/calibration_pipeline.py --run-id <RUN_ID>`

## Verify

```bash
make check
pytest tests/test_wave_f9_agent_*.py tests/test_wave_f10_agent_*.py tests/test_wave_i_calibration.py -q
PYTEST_LIVE=1 pytest tests/live/ -v
```

## Claude layout

Rules: `.claude/rules/` (delivery-invariant, no-bloat, sole-executor)
Skills: verify-after-change, live-binance-verify, calibration-wave
Agents: orchestrator, live-ops, de-bloat, strategy-calibration, verifier, delivery-guardian, strategy-auditor, delivery-debugger, data-layer-inspector

## Routing

6h/proxy → live-ops · zero-hit → strategy-calibration · wiring → strategy-auditor · delivery reject → delivery-debugger · audit → delivery-guardian · REST/WS → data-layer-inspector · F12 → de-bloat → verifier

## Guardrails

Wilder ATR/RSI; BB ddof=1; no `shift(-N)` live. Strategies in `bot/strategies/` only. Freeze: `.claude/rules/no-bloat.md`. Backlog: `docs/DEFINITION_OF_DONE.md`. Proxy: `scripts/discover_binance_proxies.py`.

## Skip

`PROJECT_AUDIT.md`, full research pack, `data/`, `telemetry/`, `.env`, monoliths unless editing.

## graphify

`graphify query "<q>"` before grep; `make graphify-update` after `bot/` edits.
