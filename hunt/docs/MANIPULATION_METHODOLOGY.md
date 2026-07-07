# PrizrakTrade Manipulation Methodology (Formalized)

## Core Principle

Manipulations are **event sequences**, not indicator values. The methodology identifies
known sequences of market events (impulse → absorption → range → sweep → breakout)
and generates trade scenarios based on the completeness and quality of the sequence.

---

## Pattern A: Absorption → Bokovik → Sweep → Pump (Long)

### Source
Transcript 1 (Yesports +150% long, BSB +100% long, Hey +100% long)

### Full Sequence
```
1. AGGRESSIVE PUMP UP      — strong impulse, large body, directional
2. ONE-CANDLE ABSORPTION   — single candle fully retraces the pump
3. BOKOVIK                 — sideway/range forms after absorption
4. SMALL IMPULSES IN RANGE — more impulses within the bokovik
5. SWEEP BELOW             — false break below bokovik to hunt stops
6. STRUCTURE BREAK UP      — BOS/CHoCH confirms the real move starts
7. ENTRY LONG
```

### Key Rules
- **"следующий памп более надёжный"**: If the first pump after bokovik
  doesn't have a clean structure break, skip it — the NEXT one is more reliable
- **Bokovik must have ≥3 touches** of range boundaries (confirms accumulation)
- **Sweep below is NOT required** if price is at the lower range boundary and
  structure is bullish — the sweep just confirms accumulation is complete
- **Entry**: bottom of bokovik OR after structure break confirmation

### Typical Targets
- TP1: nearest swing high resistance
- TP2: prior pump high (liquidity above)
- TP3: next accumulation zone above

### Stop
- 3% below bokovik low (or below the sweep low if one occurred)

---

## Pattern B: HTF Trend → Final Sweep → Exhaustion → Reversal (Short)

### Source
Transcript 2 (GTC short -30% initial, targeting -60%+)

### Full Sequence
```
1. HTF ANALYSIS            — start from higher timeframes
2. ESTABLISHED UPTREND     — HH/HL series, rising channel
3. CONSTANT IMPULSE+ABSORB  — each impulse gets absorbed
4. FINAL IMPULSE           — one that sweeps prior high
5. NO LIQUIDITY ABOVE      — no swing highs above new extreme
6. CANDLE FADE             — candles shrink, momentum dies
7. LTF CONFIRMATION        — BOS down / red impulse / structure break
8. ENTRY SHORT
```

### Key Rules
- **Start HTF → go LTF**: Always identify the HTF context first
- **FINAL impulse** is the one that takes out the LAST swing high
  (not every impulse — must be the one that exhausts remaining liquidity)
- **No liquidity above**: after the sweep, check there are no further
  swing highs to target — means the move has no more fuel
- **Candle fade**: body ratio ≤0.5 AND range ratio ≤0.6 of prior candles
- **LTF confirmation required**: never short the first red candle —
  wait for BOS or a confirmed structure break on the LTF

### Typical Targets
- TP1: nearest swing low
- TP2: bokovik low / accumulation zone below
- TP3: prior significant low

### Stop
- 3% above the sweep high

---

## Pattern C: Descending Channel → Accumulation → Breakout (Long)

### Source
Transcript 1 (BSB +250%, no initial pump version)

### Full Sequence
```
1. DESCENDING CHANNEL       — gradual lower lows, no aggressive pump
2. LIQUIDITY ACCUMULATION   — price forms a tight range at the bottom
3. BOKOVIK                  — sideway with multiple touches
4. CUMULATIVE VOLUME        — increasing volume at the bottom
5. BREAKOUT UP              — structure break, strong impulse
6. ENTRY LONG
```

### Key Rules
- Works WITHOUT an initial pump (pure accumulation play)
- Needs longer bokovik (≥5 touches) to compensate for lack of impulse
- Volume confirmation is critical (increasing volume = real accumulation)
- Targets are larger (100-250% because there's no prior pump to cap price)

---

## Global Rules (All Patterns)

### 1. HTF → LTF Flow
```
1w  → trend direction and major levels
1d  → trend confirmation, key support/resistance
4h  → intermediate structure, swing points
1h  → entry level precision, bokovik detection
15m → entry timing, sweep confirmation
5m  → micro confirmation candle
```

### 2. Liquidity Sweep Detection
A sweep requires ALL of:
- Price exceeds the swing high/low (by any amount)
- The bar's wick is ≥30% of total bar range
- Price closes back ON THE ORIGINAL side of the level
- The level was established within the lookback window

### 3. Candle Fade Detection
Compare LAST N candles vs PRIOR N candles:
- Body ratio ≤ 0.5 (bodies have shrunk by half or more)
- Range ratio ≤ 0.6 (ranges have shrunk)
- Applied AFTER a liquidity sweep (not during a range)

### 4. Confirmation
Never trade a pattern without LTF confirmation:
- Pattern A: BOS up OR choch_bull on 15m/5m
- Pattern B: BOS down OR a red impulse bar closing below the local range

### 5. Invalidation
- Pattern A: close below bokovik low + volume = zone failed
- Pattern B: close above sweep high = trend continues
- Both: unexpected volume spike in the opposite direction
