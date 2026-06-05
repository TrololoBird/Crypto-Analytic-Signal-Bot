# Forensic Archive Analysis

## Run history

| run_date | exported | SL | TP | hash | notes |
|----------|---------:|---:|---:|------|-------|
| 2026-06-05T11:15:25 | 44 | 10 | 0 | a0516e0 | initial archive seed from current bot.db |

**Archive totals:** 44 cases, 10 SL

## TIER 1 — Deterministic (n=1)

### D1 — FALSE_SIGNAL (recheck failed on confirmed data)
- `btc_correlation` **ADAUSDT** (run 2026-06-05) — TP1 was touched in-trade before stop closed — thesis held, stop placement or BE trail too tight.
- `btc_correlation` **TRUMPUSDT** (run 2026-06-05) — Detector fires on real-time unclosed candle but NOT on confirmed historical data — df[-2] fix required for btc_correlation.
- `spread_strategy` **TAOUSDT** (run 2026-06-05) — Detector fires on real-time unclosed candle but NOT on confirmed historical data — df[-2] fix required for spread_strategy.
- `spread_strategy` **PENGUUSDT** (run 2026-06-05) — Detector fires on real-time unclosed candle but NOT on confirmed historical data — df[-2] fix required for spread_strategy.

**Action:** apply confirmed-bar / df[-2] fix to listed strategies.

### D2 — confirmed_candle tracking
`confirmed_candle=0`: 3/10 (30%), unknown: 1 — within range or insufficient n.

### D3 — Ultra-fast SL (<5 min, zero MFE)
- `depth_imbalance` 1000FLOKIUSDT — 1 min, MFE=0.00

Check entry_staleness filter was active for these cases.

## TIER 2 — Case review (n=3–10)

- **spread_strategy:** 2× IMMEDIATE_ADVERSE/FALSE_SIGNAL
- **whale_walls:** 2× IMMEDIATE_ADVERSE/IMMEDIATE_ADVERSE

## TIER 3 — Statistical (deferred)

### Fix B: adaptive ATR
- Progress: `[███░░░░░░░]` 10/30 (33%)
- Status: **ACCUMULATING DATA** — ATR band SL analysis

### Fix C: regime filter
- Progress: `[██░░░░░░░░]` 10/50 (20%)
- Status: **ACCUMULATING DATA** — direction vs regime

### Fix D: score floor
- Progress: `[███░░░░░░░]` 10/30 (33%)
- Status: **ACCUMULATING DATA** — score quartile breakdown
