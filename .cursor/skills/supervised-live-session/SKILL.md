---
name: supervised-live-session
description: Runs and monitors long supervised live_watch sessions (6h), rollup reports, and session hygiene. Use when starting live_supervised_session, analyzing data/live_watch runs, or post-session ops.
---

# Supervised Live Session

## Before run

```bash
source .venv/bin/activate
python scripts/clean_session_data.py --mode smoke --config config.toml
python scripts/validate_config.py --config config.toml
python scripts/discover_binance_proxies.py   # if REST geo-blocked
```

## Run

```bash
python -m scripts.live_supervised_session --hours 6 --minutes 360 --snapshot-interval 60 --takeover
```

- `Await` until `exit_code` in terminal file
- Reap zombies: `powershell -File scripts/reap_agent_terminals.ps1` (Windows) or kill stale PIDs on macOS

## After run

```bash
python scripts/live_watch_rollup_report.py --config config.toml
```

Record `run_id` from `data/live_watch/<run_id>/`.

## Success metrics

- `delivered` count in rollup (not raw strategy hits)
- WS connected, enrichment not stuck on `pinned_fallback` only
- Exit code 0

## On REST failure

WS may work; enrichment degrades. Fix `[bot.network]` — not a reason to weaken delivery gates.
