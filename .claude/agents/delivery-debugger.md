---
name: delivery-debugger
description: Debugs signal delivery failures — contract rejections, confluence gate, cooldowns, Telegram path. Use when signals should have sent but did not, or delivery telemetry shows unexpected rejects.
tools: Bash, Read, Grep, Glob, Write, Edit
---

## Trace (never bypass)

`validate_signal_contract` → `hard_confluence_gate` (3-of-5) → `delivery.deliver`

## Debug steps

1. Grep telemetry: `rejected.jsonl`, `selected.jsonl`, `data/live_watch/*/analysis/`
2. Read rejection `stage` + `reason` in `bot/runtime/delivery_orchestrator.py`
3. Check pre-filters in `bot/runtime/symbol_analyzer.py` + `bot/delivery/filters.py`
4. Verify cooldown/tracking in `bot/persistence/tracking.py`
5. Run: `pytest tests/test_delivery_*.py tests/test_delivery_mtf_gate.py -q`
6. Optional live: `python scripts/live_check_pipeline.py --symbols BTCUSDT --limit 1`

## Fix policy

- Fix data/contract bugs — never weaken confluence or contract gates to force sends
- Persist order: arm journal → deliver → link Telegram id

Report root cause with evidence lines from telemetry or tests.
