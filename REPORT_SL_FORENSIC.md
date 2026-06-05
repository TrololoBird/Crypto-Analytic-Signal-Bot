# SL Forensic Report

**Cases analyzed:** 10

## Executive summary

- **STOP_HUNT:** 1 (10%) ← widening stops will help
- **IMMEDIATE_ADVERSE:** 7 (70%)
  - IMMEDIATE_ADVERSE: 4
  - FALSE_SIGNAL: 3 ← df[-2] fix needed
- **THESIS_FAILED:** 1 (10%) ← genuine bad calls
- **TIMING_OFF:** 1 (10%) ← SL/TTL too tight for move pace

## Per-strategy breakdown

- **whale_walls:** 4 SL → 2×IMMEDIATE_ADVERSE, 1×THESIS_FAILED, 1×CORRECT_DIRECTION_WRONG_TIMING
- **spread_strategy:** 2 SL → 2×FALSE_SIGNAL
- **btc_correlation:** 2 SL → 1×IN_TRADE_TP1_THEN_STOP, 1×FALSE_SIGNAL
- **aggression_shift:** 1 SL → 1×IMMEDIATE_ADVERSE
- **depth_imbalance:** 1 SL → 1×IMMEDIATE_ADVERSE

## Actionable recommendations by type

### THESIS_FAILED
Review setup parameters and market context filters for this pattern.

### IMMEDIATE_ADVERSE
Strategy detector fires on real-time data but NOT on confirmed historical data.
   This is a closed-candle confirmation bug in spread_strategy.
   Fix: apply confirmed-bar fix to spread_strategy.py

Review setup parameters and market context filters for this pattern.

### STOP_HUNT
SL was placed at a liquidity sweep zone. Options:
   (1) Widen ATR multiplier for btc_correlation by 1.3× in config/strategies/btc_correlation.toml
   (2) Use post-wick entry: delay entry by 1 candle after pattern fires
   (3) Place SL below the full wick low, not ATR-based

### TIMING_OFF
Thesis was directionally correct but SL/TTL too tight.
   Consider widening SL ATR multiplier or extending TTL for whale_walls.


## Cases requiring immediate fix

- TAOUSDT spread_strategy: IMMEDIATE_ADVERSE/FALSE_SIGNAL
- ADAUSDT btc_correlation: STOP_HUNT/IN_TRADE_TP1_THEN_STOP
- TRUMPUSDT btc_correlation: IMMEDIATE_ADVERSE/FALSE_SIGNAL
- PENGUUSDT spread_strategy: IMMEDIATE_ADVERSE/FALSE_SIGNAL

## known-gaps

**G1 (TP1 outcomes):** `_mark_tp1()` updates `active_signals` only; `signal_outcomes.result` is written once on close via `_close_event`. Signals that touch TP1 then close as `breakeven_stop` or `expired_active` never appear as `tp1_hit` in `signal_outcomes`. Fix belongs in `create_outcome_from_tracked()` outcome remapping, not a missing await.

## Full case cards

## Case: whale_walls SHORT BCHUSDT @ 2026-06-05T07:45:14.603616+00:00

**Verdict:** THESIS_FAILED / THESIS_FAILED
> Price continued against position with insufficient recovery — thesis failed.

### Timeline
| Event | Time | Price |
|-------|------|-------|
| Signal created | 2026-06-05T07:45:14.603616+00:00 | 224.07 |
| Position activated | 2026-06-05T07:45:59.999000+00:00 | 224.07 |
| SL hit | 2026-06-05T09:45:40.513000+00:00 | 227.37 |
| Time to entry | 0 min | — |
| Time to SL | 120 min | — |

### Setup quality
| Metric | Value | Assessment |
|--------|-------|------------|
| Score | 0.6762 | HIGH |
| ATR% | 1.4701106615977877 | moderate |
| R:R | 2.03848210328947 | GOOD |
| Entry deviation | 0.4310767530581577×ATR | FRESH |
| Confirmed candle | 0 | NO |

### Market context at signal
| BTC bias | Market regime | Direction vs bias |
|----------|---------------|-------------------|
| downtrend | volatile | ALIGNED |

### Post-SL price action
| Metric | Value |
|--------|-------|
| Max recovery after SL | 0.00% |
| Max adverse after SL | 2.24% |
| TP1 reached after SL? | NO |

### BTC correlation
| BTC move in SL candle | Direction match | BTC caused SL? |
|-----------------------|-----------------|----------------|
| 0.07% | SAME | NO |

### Strategy recheck
**Would detector fire on confirmed historical data?** N/A

### Fix recommendation
Review setup parameters and market context filters for this pattern.

---

## Case: spread_strategy SHORT TAOUSDT @ 2026-06-05T07:44:18.665813+00:00

**Verdict:** IMMEDIATE_ADVERSE / FALSE_SIGNAL
> Detector fires on real-time unclosed candle but NOT on confirmed historical data — df[-2] fix required for spread_strategy.

### Timeline
| Event | Time | Price |
|-------|------|-------|
| Signal created | 2026-06-05T07:44:18.665813+00:00 | 199.38 |
| Position activated | 2026-06-05T09:06:39.763000+00:00 | 199.38 |
| SL hit | 2026-06-05T09:29:59.999000+00:00 | 202.90464053718017 |
| Time to entry | 82 min | — |
| Time to SL | 23 min | — |

### Setup quality
| Metric | Value | Assessment |
|--------|-------|------------|
| Score | 0.6962 | HIGH |
| ATR% | 1.8247983262798881 | moderate |
| R:R | 2.1386058921834907 | GOOD |
| Entry deviation | 0.8025764015335302×ATR | FRESH |
| Confirmed candle | 0 | NO |

### Market context at signal
| BTC bias | Market regime | Direction vs bias |
|----------|---------------|-------------------|
| downtrend | volatile | ALIGNED |

### Post-SL price action
| Metric | Value |
|--------|-------|
| Max recovery after SL | 0.13% |
| Max adverse after SL | 0.01% |
| TP1 reached after SL? | NO |

### BTC correlation
| BTC move in SL candle | Direction match | BTC caused SL? |
|-----------------------|-----------------|----------------|
| -0.48% | SAME | NO |

### Strategy recheck
**Would detector fire on confirmed historical data?** NO
detector did not fire on confirmed historical slice

### Fix recommendation
Strategy detector fires on real-time data but NOT on confirmed historical data.
   This is a closed-candle confirmation bug in spread_strategy.
   Fix: apply confirmed-bar fix to spread_strategy.py

---

## Case: btc_correlation SHORT ADAUSDT @ 2026-06-05T07:45:44.564027+00:00

**Verdict:** STOP_HUNT / IN_TRADE_TP1_THEN_STOP
> TP1 was touched in-trade before stop closed — thesis held, stop placement or BE trail too tight.

### Timeline
| Event | Time | Price |
|-------|------|-------|
| Signal created | 2026-06-05T07:45:44.564027+00:00 | 0.1647 |
| Position activated | 2026-06-05T07:45:44.779000+00:00 | 0.1647 |
| SL hit | 2026-06-05T09:28:12.490000+00:00 | 0.1676459513065866 |
| Time to entry | 0 min | — |
| Time to SL | 102 min | — |

### Setup quality
| Metric | Value | Assessment |
|--------|-------|------------|
| Score | 0.6382 | MED |
| ATR% | 1.8828180785393542 | moderate |
| R:R | 1.9 | OK |
| Entry deviation | 0.41921942064649587×ATR | FRESH |
| Confirmed candle | 0 | NO |

### Market context at signal
| BTC bias | Market regime | Direction vs bias |
|----------|---------------|-------------------|
| downtrend | volatile | ALIGNED |

### Post-SL price action
| Metric | Value |
|--------|-------|
| Max recovery after SL | 1.46% |
| Max adverse after SL | 0.00% |
| TP1 reached after SL? | YES (in-trade) |

### BTC correlation
| BTC move in SL candle | Direction match | BTC caused SL? |
|-----------------------|-----------------|----------------|
| -0.48% | SAME | NO |

### Strategy recheck
**Would detector fire on confirmed historical data?** NO
detector did not fire on confirmed historical slice

### Fix recommendation
SL was placed at a liquidity sweep zone. Options:
   (1) Widen ATR multiplier for btc_correlation by 1.3× in config/strategies/btc_correlation.toml
   (2) Use post-wick entry: delay entry by 1 candle after pattern fires
   (3) Place SL below the full wick low, not ATR-based

---

## Case: aggression_shift SHORT 1000PEPEUSDT @ 2026-06-05T07:44:18.357465+00:00

**Verdict:** IMMEDIATE_ADVERSE / IMMEDIATE_ADVERSE
> Price never moved favorably; entry timing or direction was wrong.

### Timeline
| Event | Time | Price |
|-------|------|-------|
| Signal created | 2026-06-05T07:44:18.357465+00:00 | 0.0027585 |
| Position activated | 2026-06-05T09:07:18.405000+00:00 | 0.0027585 |
| SL hit | 2026-06-05T09:15:59.999000+00:00 | 0.002804587943432168 |
| Time to entry | 83 min | — |
| Time to SL | 8 min | — |

### Setup quality
| Metric | Value | Assessment |
|--------|-------|------------|
| Score | 0.6066 | MED |
| ATR% | 1.7050798249450272 | moderate |
| R:R | 1.6671842308218536 | OK |
| Entry deviation | 0.7824022931574378×ATR | FRESH |
| Confirmed candle | 0 | NO |

### Market context at signal
| BTC bias | Market regime | Direction vs bias |
|----------|---------------|-------------------|
| downtrend | volatile | ALIGNED |

### Post-SL price action
| Metric | Value |
|--------|-------|
| Max recovery after SL | 0.00% |
| Max adverse after SL | 0.25% |
| TP1 reached after SL? | NO |

### BTC correlation
| BTC move in SL candle | Direction match | BTC caused SL? |
|-----------------------|-----------------|----------------|
| 0.03% | OPPOSITE | NO |

### Strategy recheck
**Would detector fire on confirmed historical data?** N/A

### Fix recommendation
Review setup parameters and market context filters for this pattern.

---

## Case: btc_correlation SHORT TRUMPUSDT @ 2026-06-05T07:46:14.435165+00:00

**Verdict:** IMMEDIATE_ADVERSE / FALSE_SIGNAL
> Detector fires on real-time unclosed candle but NOT on confirmed historical data — df[-2] fix required for btc_correlation.

### Timeline
| Event | Time | Price |
|-------|------|-------|
| Signal created | 2026-06-05T07:46:14.435165+00:00 | 1.677 |
| Position activated | 2026-06-05T08:11:06.857000+00:00 | 1.677 |
| SL hit | 2026-06-05T09:15:03.458000+00:00 | 1.705 |
| Time to entry | 24 min | — |
| Time to SL | 64 min | — |

### Setup quality
| Metric | Value | Assessment |
|--------|-------|------------|
| Score | 0.6886 | HIGH |
| ATR% | 1.8266019665362374 | moderate |
| R:R | 2.043750971634472 | GOOD |
| Entry deviation | 0.16322738418350288×ATR | FRESH |
| Confirmed candle | 0 | NO |

### Market context at signal
| BTC bias | Market regime | Direction vs bias |
|----------|---------------|-------------------|
| downtrend | volatile | ALIGNED |

### Post-SL price action
| Metric | Value |
|--------|-------|
| Max recovery after SL | 0.12% |
| Max adverse after SL | 0.12% |
| TP1 reached after SL? | NO |

### BTC correlation
| BTC move in SL candle | Direction match | BTC caused SL? |
|-----------------------|-----------------|----------------|
| 0.03% | OPPOSITE | NO |

### Strategy recheck
**Would detector fire on confirmed historical data?** NO
detector did not fire on confirmed historical slice

### Fix recommendation
Strategy detector fires on real-time data but NOT on confirmed historical data.
   This is a closed-candle confirmation bug in btc_correlation.
   Fix: apply confirmed-bar fix to btc_correlation.py

---

## Case: spread_strategy SHORT PENGUUSDT @ 2026-06-05T07:44:08.360911+00:00

**Verdict:** IMMEDIATE_ADVERSE / FALSE_SIGNAL
> Detector fires on real-time unclosed candle but NOT on confirmed historical data — df[-2] fix required for spread_strategy.

### Timeline
| Event | Time | Price |
|-------|------|-------|
| Signal created | 2026-06-05T07:44:08.360911+00:00 | 0.006453 |
| Position activated | 2026-06-05T08:37:59.999000+00:00 | 0.006453 |
| SL hit | 2026-06-05T09:15:01.219000+00:00 | 0.006579 |
| Time to entry | 53 min | — |
| Time to SL | 37 min | — |

### Setup quality
| Metric | Value | Assessment |
|--------|-------|------------|
| Score | 0.5861 | MED |
| ATR% | 1.8579223273584806 | moderate |
| R:R | 1.8982856899203546 | OK |
| Entry deviation | 0.2168623347120848×ATR | FRESH |
| Confirmed candle | 0 | NO |

### Market context at signal
| BTC bias | Market regime | Direction vs bias |
|----------|---------------|-------------------|
| downtrend | volatile | ALIGNED |

### Post-SL price action
| Metric | Value |
|--------|-------|
| Max recovery after SL | 0.00% |
| Max adverse after SL | 0.31% |
| TP1 reached after SL? | NO |

### BTC correlation
| BTC move in SL candle | Direction match | BTC caused SL? |
|-----------------------|-----------------|----------------|
| 0.03% | SAME | NO |

### Strategy recheck
**Would detector fire on confirmed historical data?** NO
detector did not fire on confirmed historical slice

### Fix recommendation
Strategy detector fires on real-time data but NOT on confirmed historical data.
   This is a closed-candle confirmation bug in spread_strategy.
   Fix: apply confirmed-bar fix to spread_strategy.py

---

## Case: whale_walls SHORT LINKUSDT @ 2026-06-05T07:45:41.748243+00:00

**Verdict:** IMMEDIATE_ADVERSE / IMMEDIATE_ADVERSE
> Price never moved favorably; entry timing or direction was wrong.

### Timeline
| Event | Time | Price |
|-------|------|-------|
| Signal created | 2026-06-05T07:45:41.748243+00:00 | 7.541 |
| Position activated | 2026-06-05T09:06:23.746000+00:00 | 7.541 |
| SL hit | 2026-06-05T09:15:00.378000+00:00 | 7.643 |
| Time to entry | 80 min | — |
| Time to SL | 9 min | — |

### Setup quality
| Metric | Value | Assessment |
|--------|-------|------------|
| Score | 0.6436000000000001 | MED |
| ATR% | 1.3805037155535351 | moderate |
| R:R | 2.1124188535375787 | GOOD |
| Entry deviation | 0.950974067075629×ATR | FRESH |
| Confirmed candle | 0 | NO |

### Market context at signal
| BTC bias | Market regime | Direction vs bias |
|----------|---------------|-------------------|
| downtrend | volatile | ALIGNED |

### Post-SL price action
| Metric | Value |
|--------|-------|
| Max recovery after SL | 0.00% |
| Max adverse after SL | 0.44% |
| TP1 reached after SL? | NO |

### BTC correlation
| BTC move in SL candle | Direction match | BTC caused SL? |
|-----------------------|-----------------|----------------|
| 0.03% | SAME | NO |

### Strategy recheck
**Would detector fire on confirmed historical data?** N/A

### Fix recommendation
Review setup parameters and market context filters for this pattern.

---

## Case: depth_imbalance SHORT 1000FLOKIUSDT @ 2026-06-05T09:09:41.417126+00:00

**Verdict:** IMMEDIATE_ADVERSE / IMMEDIATE_ADVERSE
> Price never moved favorably; entry timing or direction was wrong.

### Timeline
| Event | Time | Price |
|-------|------|-------|
| Signal created | 2026-06-05T09:09:41.417126+00:00 | 0.02375 |
| Position activated | 2026-06-05T09:12:58.201000+00:00 | 0.02375 |
| SL hit | 2026-06-05T09:14:04.473000+00:00 | 0.024 |
| Time to entry | 3 min | — |
| Time to SL | 1 min | — |

### Setup quality
| Metric | Value | Assessment |
|--------|-------|------------|
| Score | 0.5452 | LOW |
| ATR% | 1.3110492726342209 | moderate |
| R:R | 2.0442459999999976 | GOOD |
| Entry deviation | 0.7065453669887585×ATR | FRESH |
| Confirmed candle | 0 | NO |

### Market context at signal
| BTC bias | Market regime | Direction vs bias |
|----------|---------------|-------------------|
| downtrend | volatile | ALIGNED |

### Post-SL price action
| Metric | Value |
|--------|-------|
| Max recovery after SL | 0.00% |
| Max adverse after SL | 0.84% |
| TP1 reached after SL? | NO |

### BTC correlation
| BTC move in SL candle | Direction match | BTC caused SL? |
|-----------------------|-----------------|----------------|
| 0.03% | SAME | NO |

### Strategy recheck
**Would detector fire on confirmed historical data?** N/A

### Fix recommendation
Review setup parameters and market context filters for this pattern.

---

## Case: whale_walls SHORT ZECUSDT @ 2026-06-05T07:44:08.703904+00:00

**Verdict:** IMMEDIATE_ADVERSE / IMMEDIATE_ADVERSE
> Price never moved favorably; entry timing or direction was wrong.

### Timeline
| Event | Time | Price |
|-------|------|-------|
| Signal created | 2026-06-05T07:44:08.703904+00:00 | 303.18 |
| Position activated | 2026-06-05T07:53:59.999000+00:00 | 303.18 |
| SL hit | 2026-06-05T08:06:59.999000+00:00 | 331.7325126997824 |
| Time to entry | 9 min | — |
| Time to SL | 13 min | — |

### Setup quality
| Metric | Value | Assessment |
|--------|-------|------------|
| Score | 0.6292 | MED |
| ATR% | 8.969216055771474 | high vol |
| R:R | 1.9000000000000004 | OK |
| Entry deviation | 0.200787932756744×ATR | FRESH |
| Confirmed candle | 0 | NO |

### Market context at signal
| BTC bias | Market regime | Direction vs bias |
|----------|---------------|-------------------|
| downtrend | volatile | ALIGNED |

### Post-SL price action
| Metric | Value |
|--------|-------|
| Max recovery after SL | 0.24% |
| Max adverse after SL | 8.58% |
| TP1 reached after SL? | NO |

### BTC correlation
| BTC move in SL candle | Direction match | BTC caused SL? |
|-----------------------|-----------------|----------------|
| -0.48% | OPPOSITE | NO |

### Strategy recheck
**Would detector fire on confirmed historical data?** N/A

### Fix recommendation
Review setup parameters and market context filters for this pattern.

---

## Case: whale_walls SHORT ASTERUSDT @ 2026-06-05T02:37:30.815614+00:00

**Verdict:** TIMING_OFF / CORRECT_DIRECTION_WRONG_TIMING
> Thesis validated (TP1 in 15 candles) but SL was too tight for the move's timing.

### Timeline
| Event | Time | Price |
|-------|------|-------|
| Signal created | 2026-06-05T02:37:30.815614+00:00 | 0.6587 |
| Position activated | 2026-06-05T02:42:04.609000+00:00 | 0.6587 |
| SL hit | 2026-06-05T02:52:11.773000+00:00 | 0.6656 |
| Time to entry | 4 min | — |
| Time to SL | 10 min | — |

### Setup quality
| Metric | Value | Assessment |
|--------|-------|------------|
| Score | 0.6628000000000001 | HIGH |
| ATR% | 0.6927537770208755 | low vol |
| R:R | 2.044857246376809 | GOOD |
| Entry deviation | 0.5259502622033028×ATR | FRESH |
| Confirmed candle | 0 | NO |

### Market context at signal
| BTC bias | Market regime | Direction vs bias |
|----------|---------------|-------------------|
| downtrend | volatile | ALIGNED |

### Post-SL price action
| Metric | Value |
|--------|-------|
| Max recovery after SL | 3.26% |
| Max adverse after SL | 1.64% |
| TP1 reached after SL? | YES in 15 candles |

### BTC correlation
| BTC move in SL candle | Direction match | BTC caused SL? |
|-----------------------|-----------------|----------------|
| -0.19% | OPPOSITE | NO |

### Strategy recheck
**Would detector fire on confirmed historical data?** N/A

### Fix recommendation
Thesis was directionally correct but SL/TTL too tight.
   Consider widening SL ATR multiplier or extending TTL for whale_walls.

---
