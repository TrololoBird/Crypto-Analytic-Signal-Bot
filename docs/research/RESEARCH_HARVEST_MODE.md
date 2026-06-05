# Research harvest mode

> **Calibration is intentionally last.** Run harvest after architecture, data plane, and strategy logic are stable.

## Purpose

Fixed **~10 liquid USD-M symbols**, maximum public data capture, **no Telegram ACTION**, full `strategy_decisions` + prepared-context snapshots. Use output to design or rewrite strategies — not for threshold calibration.

## Run

```bash
python scripts/clean_session_data.py --mode smoke --config config.toml
python main.py harvest --config config.toml --minutes 60
# or
make research-harvest
python scripts/research_harvest_session.py --minutes 120 --symbols BTCUSDT ETHUSDT
```

## Output

| Path | Content |
|------|---------|
| `data/research_harvest/{run_id}/manifest.json` | Session meta |
| `data/research_harvest/{run_id}/cycles.jsonl` | Per-cycle funnel + prepared snapshot |
| `data/research_harvest/{run_id}/symbols/{SYM}/cycles.jsonl` | Per-symbol stream |
| `data/bot/telemetry/runs/{run_id}/` | Standard telemetry (`strategy_decisions.jsonl`, …) |

## Default symbols

`BTCUSDT`, `ETHUSDT`, `SOLUSDT`, `XRPUSDT`, `BNBUSDT`, `DOGEUSDT`, `LINKUSDT`, `XAUUSDT`, `XAGUSDT`, `PAXGUSDT` (+ required pins merged).

## Config (`[bot.research_harvest]`)

Set `enabled = true` only if you want harvest profile on normal `run` (prefer CLI `harvest`).

## vs production `run`

| | `run` | `harvest` |
|---|--------|-----------|
| Shortlist | 40–55 dynamic | Fixed 10 |
| Telegram | Optional | Off |
| Delivery | ACTION/WATCH | Skipped (candidates logged) |
| Strategies | Lanes | `route_all_enabled_strategies` |
| Calibration | Later wave | **Not in this mode** |
