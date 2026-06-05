---
name: validate-delivery-path
description: Audits signal delivery integrity — contract validation, confluence gate, cooldowns. Use before merging delivery changes or when investigating signals that should not have been sent.
---

# Validate Delivery Path

## Required order in `delivery_orchestrator.py`

1. `validate_signal_contract(signal)` — must run first
2. `_hard_confluence_gate(signal, prepared)` — 3-of-5 required
3. Cooldown / blacklist checks
4. `delivery.deliver(...)`

## Grep audit

Search for any path calling `delivery.deliver` or Telegram send without prior contract validation.

## Live verification

```bash
python scripts/live_check_pipeline.py --symbols BTCUSDT --limit 1
python scripts/live_smoke_bot.py --warmup-seconds 30 --keep-session-data
```

Review telemetry for `hard_confluence_gate_failed` and contract issue codes.

## Critical bug

Any code path reaching Telegram/delivery **without** contract + confluence gates is a **CRITICAL BUG** — fix immediately.
