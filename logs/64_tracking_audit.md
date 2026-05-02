# 64 Tracking Lifecycle Audit

## Confirmed facts

- Database file inspected: `data/bot/bot.db`.
- Runtime tracking table is `active_signals`; the legacy `signals` table exists but had no usable `status` column for the active-signal lifecycle.
- Before cleanup, open rows in `active_signals` were:
  - `SKYAIUSDT` / `wick_trap_reversal` / `long`, active, created `20260502T021848092489Z`
  - `1000LUNCUSDT` / `bos_choch` / `short`, active, created `20260502T021907409604Z`
  - `WLDUSDT` / `bos_choch` / `short`, pending, created `20260502T031521498538Z`
- At audit time those rows were older than 4 hours and were stale by the mission rule.
- Cooldowns contained `27` rows across `20` distinct symbols, so not all 45 shortlist symbols were in cooldown.

## Root cause contribution

- Active signal expiry was configured at `720` minutes, allowing stale open rows to survive restarts for 12 hours.
- The previous run had rejected all selected signals with `symbol_has_open_signal`; the stale rows were therefore a confirmed blocker for those symbols.
- This stale tracking state did not explain every later zero-candidate result after cleanup, but it was a confirmed lifecycle bug that could block symbols across runs.

## Remediation

- Closed the stale open rows directly in SQLite as `status='closed'`, `close_reason='expired'`, with `closed_at=2026-05-02T09:04:03Z`.
- Reduced `active_expiry_minutes` to a hard 4-hour maximum in `config.toml`, `config.toml.example`, `bot/config.py`, and `bot/tracking.py`.
- Added startup cleanup through `MemoryRepository.expire_open_signals_older_than(max_age_minutes=240)`.
- Added startup cooldown cleanup through `MemoryRepository.purge_cooldowns_older_than(max_age_minutes=120)`.
- Startup now logs `startup stale state cleanup | expired_open_signals=... purged_cooldowns=...`.

## Post-fix state

- The 10-minute run `data/bot/logs/bot_20260502_095033_25864.log` ended with `0` open active/pending signals and `0` cooldown rows.
- Stale signals no longer block symbols indefinitely on restart.
