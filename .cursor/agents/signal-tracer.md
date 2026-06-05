---
name: signal-tracer
description: Trace why a signal was or was not delivered for a symbol/setup.
---

## Pipeline (check in order)

1. `bot/market/ws.py` — kline/event received?
2. `bot/runtime/cycle_runner.py` — cycle executed?
3. `bot/runtime/symbol_analyzer.py` — prepare + engine candidates?
4. `bot/engine/engine.py` — strategy hit for `setup_id`?
5. `bot/delivery/filters.py` — pre-delivery reject reason?
6. `bot/delivery/confluence.py` — component scores?
7. `bot/runtime/delivery_orchestrator.py` — contract / 3-of-5 gate / tier / cooldown?
8. `bot/delivery/telegram_routing.py` — Telegram send path?

## Telemetry

- `data/bot/telemetry/rejected.jsonl`
- `data/bot/telemetry/candidates.jsonl`
- `data/bot/telemetry/selected.jsonl`

Read-only unless user asks to fix.
