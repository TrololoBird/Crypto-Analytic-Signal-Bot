# Live smoke session

Short live validation. Agent owns terminal lifecycle.

## Prep

1. `source .venv/bin/activate`
2. `python scripts/clean_session_data.py --mode smoke --config config.toml`
3. Ensure `provider = "none"` or `BOT_NOTIFIER_PROVIDER=none` unless Telegram test is intended
4. `python scripts/probe_binance_access.py --all-configured` — note REST/WS status

## Run

```bash
make live-smoke
```

Or: `python scripts/live_smoke_bot.py --warmup-seconds 30` (add `--keep-session-data` only if debugging)

## After

- Summarize: cycles, delivered count, WS connected, enrichment mode
- If zero deliveries: check telemetry rejection reasons (do not weaken gates blindly)
- Point to `data/live_watch/` or logs path

Skill: `supervised-live-session` for long runs.
