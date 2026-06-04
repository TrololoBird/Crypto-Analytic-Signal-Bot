---
name: live-binance-verify
description: Runs live Binance public API checks after changes to bot/market, bot/features, bot/runtime, or pipeline. Use when validating REST, WS, indicators, or live_check scripts.
---

Same workflow as `.cursor/skills/live-binance-verify/SKILL.md`.

Prereq: `PYTEST_LIVE=1`, REST reachable or document proxy/geo block.

Quick: `pytest tests/live/ -v -m live`

Full: compileall → validate_config → live pytest → live_check_pipeline

Never weaken delivery gates to pass tests.
