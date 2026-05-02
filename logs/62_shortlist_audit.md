# DIAG_3 shortlist audit

Timestamp: 2026-05-02T09:21:50Z

## Confirmed facts

- DB cleanup left `active_signals` with zero open tracked signals.
- Cooldowns at DIAG_1 time: 27 rows, 20 distinct symbols. That is not all 45 shortlist symbols.
- Shortlist failure observed in DIAG_2 live-run:

```text
UniverseSymbol.__init__() got an unexpected keyword argument 'strategy_fits'
'UniverseSymbol' object has no attribute 'strategy_fits'
```

- Root cause: `bot.universe.build_shortlist()` writes and reads `UniverseSymbol.strategy_fits`, but `bot.models.UniverseSymbol` did not define that field.

## Code change

- Added `strategy_fits: tuple[str, ...] = ()` to `bot.models.UniverseSymbol`.

## Live-run verification

Run: `data/bot/logs/bot_20260502_092004_20848.log`

- Initial full shortlist:
  - `source=rest_full`
  - `size=45`
  - `eligible=131`
  - `dynamic_pool=120`
  - `pinned=4`
  - `avg_score=0.78128`
- Light refresh after WS warmed:
  - `source=ws_light`
  - `size=45`
  - `eligible=70`, later `132`
  - `pinned=4`
  - `strategy_seed=15`
  - all 15 setup ids had non-zero `strategy_fit_counts`.
- Runtime cycles occurred across the 45-symbol shortlist.
- Telemetry evidence:
  - `data/bot/telemetry/runs/20260502_092004_20848/analysis/shortlist.jsonl`
  - `data/bot/telemetry/runs/20260502_092004_20848/analysis/cycles.jsonl`

## Remaining findings

- `post_filter_candidates` remained 0 in the short live-run even when `raw_hits` appeared. That points downstream of shortlist and detector execution, likely confirmation/filter/rejection logic.
- WS message buffer overflow warnings appeared with full 45-symbol public bookTicker flow:

```text
message buffer full | dropped=9001 processed=14309
```

This did not stop cycles in the short run, but it is a confirmed pressure point for longer validation.
