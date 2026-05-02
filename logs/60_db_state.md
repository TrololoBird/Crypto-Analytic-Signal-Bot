# DIAG_1 DB state

Timestamp: 2026-05-02T09:04:03Z

## Confirmed facts

- Runtime DB path: `data/bot/bot.db`.
- The directive query against `signals.status` did not match the actual schema. The runtime tracking table is `active_signals`.
- `signals` table has no `status` column and is empty in this DB.
- `active_signals` before cleanup:
  - `active`: 2
  - `pending`: 1
  - `closed`: 26
- Open tracked rows before cleanup:
  - `SKYAIUSDT|wick_trap_reversal|long|20260502T021848092489Z`, status `active`, age about 6.7h.
  - `1000LUNCUSDT|bos_choch|short|20260502T021907409604Z`, status `active`, age about 6.7h.
  - `WLDUSDT|bos_choch|short|20260502T031521498538Z`, status `pending`, age about 5.8h.
- All three rows were older than the 4h stale threshold from the directive.
- `cooldowns` has no `cooldown_until` column. Actual columns are `cooldown_key`, `last_sent_at`, `setup_id`, `symbol`, `cooldown_type`.
- Cooldowns before cleanup: 27 rows, 20 distinct symbols. This is not "all 45 symbols in cooldown".

## Action taken

Because `active_signals.status` supports `pending`, `active`, and `closed`, stale rows were expired using the runtime-compatible terminal state:

```sql
UPDATE active_signals
SET status = 'closed',
    closed_at = '<cleanup timestamp>',
    close_reason = 'expired',
    close_price = COALESCE(last_price, activation_price, entry_mid, close_price)
WHERE status IN ('pending','active')
  AND created_at < '<now - 4 hours>';
```

Rows changed: 3.

## Post-cleanup verification

- Open tracked rows after cleanup: 0.
- `active_signals` status counts after cleanup:
  - `closed`: 29
- Cooldowns were not globally cleared in DIAG_1 because coverage was 20 distinct symbols, not all 45.

## Inference

The stale open signals were a confirmed blocker for any delivery path that rejects `symbol_has_open_signal`, but they do not fully explain `cycles=0` by themselves. `cycles=0` still requires WS/shortlist/kline pipeline investigation.
