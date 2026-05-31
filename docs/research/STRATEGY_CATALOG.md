# Strategy Catalog (38 detectors)

Web-verified taxonomy for a Binance USD-M **public** Telegram signal bot.  
**Not** tied to current `bot2` implementation thresholds — calibrate live against this doc.

See also: [GAP_ANALYSIS_BOT2.md](GAP_ANALYSIS_BOT2.md), [BINANCE_PUBLIC_DATA_MATRIX.md](BINANCE_PUBLIC_DATA_MATRIX.md).

## Evidence levels

- **A** — Widely documented (SMC, Wyckoff, classic TA, exchange analytics).
- **B** — Valid with filters (funding, sessions, sentiment extremes).
- **C** — Microstructure; use as **confluence on 15m close**, not sole ACTION trigger.

## Summary matrix

| # | setup_id | Name | Lvl | trigger | pattern | required_tfs | Binance public data |
|---|----------|------|-----|---------|---------|--------------|---------------------|
| 1 | structure_pullback | Trend pullback | A | 15m | 15m | 1h,4h,15m | klines |
| 2 | structure_break_retest | Break & retest | A | 15m | 1h/15m | 1h,15m | klines |
| 3 | wick_trap_reversal | Wick trap | A | 15m | 15m | 15m,1h | klines |
| 4 | squeeze_setup | Vol squeeze | A | 15m | 15m | 15m,1h | klines |
| 5 | ema_bounce | EMA pullback | A | 15m | 15m | 1h,15m | klines |
| 6 | fvg_setup | Fair Value Gap | A | 15m | 15m | 1h,4h,15m | klines |
| 7 | order_block | Order block | A | 15m | 15m | 1h,15m | klines |
| 8 | liquidity_sweep | Liquidity sweep | A | 15m | 15m | 1h,15m | klines |
| 9 | bos_choch | BOS / CHoCH | A | 15m | 15m | 1h,15m | klines |
| 10 | hidden_divergence | Hidden RSI div | A | 15m | 15m | 1h,15m | klines |
| 11 | indicator_divergence | Multi-indicator div | A | 15m | 15m | 15m,1h | klines, aggTrade |
| 12 | funding_reversal | Funding fade | B | 1h/15m | 15m | 1h,15m | funding, OI |
| 13 | cvd_divergence | CVD divergence | A | 15m | 15m | 15m | aggTrade |
| 14 | session_killzone | Kill zone | B | 15m+clock | 15m | 1h,15m | klines, time |
| 15 | breaker_block | Breaker block | A | 15m | 15m | 1h,15m | klines |
| 16 | turtle_soup | Turtle soup | A | 15m | 1h/15m | 1h,15m | klines |
| 17 | vwap_trend | VWAP reclaim | A | 15m | 15m | 15m | klines |
| 18 | supertrend_follow | Supertrend pullback | A | 15m | 15m | 4h,1h,15m | klines |
| 19 | multi_tf_trend | Multi-TF trend | A | 15m | 4h/1h | 4h,1h,15m | klines |
| 20 | price_velocity | Price impulse | A | 15m | 15m | 15m | klines |
| 21 | volume_anomaly | Volume spike | A | 15m | 15m | 15m | klines |
| 22 | volume_climax_reversal | Volume climax | A | 15m | 15m | 1h,15m | klines |
| 23 | keltner_breakout | Keltner break | A | 15m | 15m | 15m,1h | klines |
| 24 | bb_squeeze | BB squeeze | A | 15m | 15m | 15m,4h | klines |
| 25 | atr_expansion | ATR expansion | A | 15m | 15m | 15m | klines |
| 26 | whale_walls | Whale walls | C→B | 15m | 15m | 15m | depth |
| 27 | spread_strategy | Spread break | C→B | 15m | 15m | 15m | bookTicker |
| 28 | depth_imbalance | Depth imbalance | C→B | 15m | 15m | 15m | depth |
| 29 | absorption | Absorption | A | 15m | 15m | 15m | aggTrade |
| 30 | aggression_shift | Aggression shift | B | 15m | 15m | 15m | aggTrade, taker ratio |
| 31 | liquidation_heatmap | Liq fade | B | 15m | 15m | 15m,1h | forceOrder, OI |
| 32 | stop_hunt_detection | Stop hunt | A | 15m | 15m | 15m,1h | klines |
| 33 | oi_divergence | OI divergence | A | 4h/15m | 15m | 4h,15m | OI, OI hist |
| 34 | ls_ratio_extreme | L/S extreme | B | 4h | 15m | 4h,15m | global L/S |
| 35 | rsi_divergence_bottom | RSI bottom div | A | 15m | 15m | 1h,15m | klines |
| 36 | wyckoff_spring | Wyckoff spring | A | 15m | 1h | 1h,15m | klines |
| 37 | btc_correlation | BTC correlation | B | 15m | 15m | BTC 1h + alt 15m | multi klines |
| 38 | altcoin_season_index | Altseason | B | 1d/4h | 1h | 1d,4h,1h | ticker24h |

---

## Global trade plan defaults

| Parameter | Majors | Alts |
|-----------|--------|------|
| Entry zone width | 0.15–0.35% | 0.25–0.50% |
| Scale-in legs | 3 weights 50/30/20 | same |
| SL buffer | 0.4–0.8 × ATR14 | same |
| TP ladder | 1.5R / 2.5R / 4R | same |
| Min R:R TP1 | ≥ 1.5 | ≥ 1.5 |
| TTL | 4–12 × pattern_tf bars | same |

**confirmation_profile** mapping: `trend_follow`, `breakout_acceptance`, `countertrend_exhaustion`, `divergence_reversal`.

---

## SMC / Structure

### 1. `structure_pullback`

| Field | Detail |
|-------|--------|
| Level | A |
| Concept | Pullback to structure/EMA in HTF trend ([United Kings SMC](https://unitedkings.net/smart-money-concepts-complete-smc-guide/)). |
| Regime | Trending ADX 1h > 15–18; avoid mid-range chop. |
| Detection | HTF HH/HL or LH/LL; 15m pullback 5–12 bars; rejection candle; volume_ratio ≥ 0.85. |
| Entry | Lower/upper third of pullback zone. |
| SL | Beyond pullback swing + ATR buffer. |
| TP | Prior swing extension; min 1.9R TP1. |
| Invalidation | 15m close through structure. |
| WATCH | Pattern without volume. |
| ACTION | + HTF bias + R:R. |
| Anti | Counter-trend without CHoCH. |

### 2. `structure_break_retest`

| Field | Detail |
|-------|--------|
| Level | A |
| Concept | Break level on 1h, retest on 15m (classic TA + SMC). |
| Detection | 1h close beyond level; displacement; 15m retest + rejection. |
| Entry | Broken level zone. |
| SL | Beyond retest wick. |
| TP | Measured move or next liquidity. |
| Anti | Break on wick only; heavy volume retest (often reversal). |

### 3. `wick_trap_reversal`

| Field | Detail |
|-------|--------|
| Level | A |
| Concept | Stop raid wick, close back inside ([fxnx order flow](https://fxnx.com/en/blog/order-flow-trading-the-truth-behind-the-wick)). |
| Detection | Wick ≥ 1.5× ATR14 through level; favorable close_position; vol ≥ 1.0× MA. |
| SL | Beyond wick extreme. |
| TP | Range mid → opposite liquidity. |

### 4. `squeeze_setup`

| Field | Detail |
|-------|--------|
| Level | A |
| Concept | Volatility compression then expansion (BB/Keltner family). |
| Detection | Bandwidth/ATR bottom 10–20%; breakout close; volume ≥ 1.3× MA. |
| Anti | Break without volume ([CryptoFutures BB](https://cryptofutures.trading/Bollinger_Band_Squeeze_Strategy)). |

### 5. `fvg_setup`

| Field | Detail |
|-------|--------|
| Level | A |
| Concept | 3-candle gap; trade mitigation at CE 50% ([ICT FVG](https://www.ictkillzone.com/ict-fair-value-gap), [TSG FVG](https://tradingstrategyguides.com/day-6-fair-value-gaps-explained-ict-smc-fvg-trading-guide/)). |
| Detection | Clean gap (no wick overlap c1/c3); HTF premium/discount; optional sweep+MSS; price in gap. |
| Entry | CE limit or rejection inside gap. |
| SL | Far edge of FVG. |
| TTL | ~20×15m bars. |
| Anti | Mitigated gap; counter HTF; outside killzone (product rule). |

### 6. `order_block`

| Field | Detail |
|-------|--------|
| Level | A |
| Concept | Last opposing candle before displacement + BOS ([Trading Wyckoff SMC](https://tradingwyckoff.com/en/smart-money-concepts/)). |
| Detection | Impulse breaks structure; retest OB; rejection. |
| SL | Beyond OB. |

### 7. `liquidity_sweep`

| Field | Detail |
|-------|--------|
| Level | A |
| Concept | Sweep swing liquidity then reclaim ([United Kings](https://unitedkings.net/smart-money-concepts-complete-smc-guide/)). |
| Detection | Pierce swing; close back in range; optional CHoCH. |
| TP | Opposite range side. |

### 8. `bos_choch`

| Field | Detail |
|-------|--------|
| Level | A |
| Concept | BOS = continuation; CHoCH = character change (SMC). |
| Role | Often **direction filter**; trade entry via OB/FVG retest after MSS. |

### 9. `breaker_block`

| Field | Detail |
|-------|--------|
| Level | A |
| Concept | Failed OB after sweep + body close + MSS ([ICT breaker](https://innercircletrader.net/tutorials/ict-breaker-block-trading/), [Alchemy](https://alchemymarkets.com/education/strategies/breaker-block-explained/)). |
| Anti | Break without prior sweep (mitigation block only). |

### 10. `turtle_soup`

| Field | Detail |
|-------|--------|
| Level | A |
| Concept | False breakout of ≥20-bar extreme ([Alchemy](https://alchemymarkets.com/education/strategies/turtle-soup-strategy/), [ICT](https://innercircletrader.net/tutorials/ict-turtle-soup-pattern/)). |
| SL | Tight above/below false extreme. |
| TP | Opposite range (1:3–1:4 R:R literature). |

---

## Trend continuation

### 11. `ema_bounce`

| Field | Detail |
|-------|--------|
| Level | A |
| Concept | Touch EMA20/50 in trend; classic TA. |
| Detection | 1h trend; 15m touch within 0.5–1.5% of EMA; directional close. |

### 12. `vwap_trend`

| Field | Detail |
|-------|--------|
| Level | A |
| Concept | Session VWAP reclaim ([TradeDisciple](https://tradedisciple.com/blog/vwap-trading-guide), [Tradapt](https://www.tradapt.com/resources/strategies/vwap-reclaim)). |
| Detection | Was below VWAP 15–30m; close above; vol ≥ 1.2× session avg. |
| Alt subtype | 2σ mean reversion only if ADX < 25 — tag `vwap_reversion` in reasons. |

### 13. `supertrend_follow`

| Field | Detail |
|-------|--------|
| Level | A |
| Concept | Pullback to Supertrend line ([Alchemy ST](https://alchemymarkets.com/education/indicators/supertrend-explained/), [crypto settings](https://cryptotrading-guide.com/best-supertrend-settings-for-crypto-2026-atr-length-multiplier/)). |
| Params | ATR 10–14, mult 3–4 crypto. |
| Anti | Frequent ST flips (range). |

### 14. `multi_tf_trend`

| Field | Detail |
|-------|--------|
| Level | A |
| Concept | HTF EMA50 slope + bias votes + 15m pullback RSI (long < 58). |
| required_tfs | **All** 4h, 1h, 15m mandatory. |

### 15. `hidden_divergence`

| Field | Detail |
|-------|--------|
| Level | A |
| Concept | Continuation: HL price + LL RSI ([Kraken](https://www.kraken.com/learn/rsi-divergences-what-they-how-they-work), [trendsandbreakouts](https://trendsandbreakouts.com/rsi-divergence-regular-hidden)). |
| Anti | Confuse with regular reversal divergence. |

---

## Breakout / volatility

### 16. `bb_squeeze`

| Field | Detail |
|-------|--------|
| Level | A |
| Detection | BB width < p10; close outside band; vol ≥ 1.5× ([Tapbit 2026](https://blog.tapbit.com/ja/how-to-use-bollinger-bands-squeeze-for-crypto-trading-2026-btc-altcoin-breakout-guide/)). |
| Params | BB 20, 2 std ddof=1. |

### 17. `keltner_breakout`

| Field | Detail |
|-------|--------|
| Level | A |
| Params | EMA 20, ATR mult 1.5–2.0; break + retest. |

### 18. `atr_expansion`

| Field | Detail |
|-------|--------|
| Level | A |
| Detection | Bar range > 1.8–2.5 × ATR14 after compression. |
| Anti | News spikes without calendar filter. |

### 19. `price_velocity`

| Field | Detail |
|-------|--------|
| Level | A |
| Detection | Body/range > 70%; ROC10 > 0.15–0.5% on 15m; volume confirm. |

### 20. `volume_anomaly`

| Field | Detail |
|-------|--------|
| Level | A |
| Detection | volume_ratio20 ≥ 2.0 + directional body. |
| Tier | Often WATCH «activity» unless break structure. |

### 21. `volume_climax_reversal`

| Field | Detail |
|-------|--------|
| Level | A |
| Concept | Wyckoff stopping volume / climax bar. |
| Detection | Vol top 5% of 50 bars + long wick + close off extreme. |

---

## Reversal / exhaustion

### 22. `rsi_divergence_bottom`

| Field | Detail |
|-------|--------|
| Level | A |
| Concept | Regular bullish div: LL price, HL RSI at support. |
| Detection | RSI was < 35; divergence 10–25 bars; confirm candle. |

### 23. `indicator_divergence`

| Field | Detail |
|-------|--------|
| Level | A |
| Detection | 2-of-4: RSI, MACD hist, OBV, delta_ratio vs price ([Quantum](https://www.quantum-algo.com/blog/rsi-divergence-strategy-guide/)). |
| ACTION | Requires level + not fighting HTF. |

### 24. `funding_reversal`

| Field | Detail |
|-------|--------|
| Level | B |
| Concept | Fade crowded funding z-score ([Quant Journey](https://quantjourney.substack.com/p/funding-rates-in-crypto-the-hidden), [Adeline117 backtest](https://github.com/Adeline117/Strategy-project)). |
| Detection | \|z\| > 2.5 (60d window); 15m reversal bar; OI Δ ≥ 0.5–1%. |
| TTL | ~8h hold equivalent. |
| Anti | Extreme funding persists days — need SL discipline ([TraderSpy](https://blog.traderspy.app/en/blog/crypto-funding-rates-secret-weapon/)). |

### 25. `cvd_divergence`

| Field | Detail |
|-------|--------|
| Level | A |
| Concept | Price vs CVD disagree ([Kalena](https://blog.kalena.ai/cumulative-volume-delta-strategy-the-divergence-playbook-that-stops-you-trading-against-the-market), [TRDR](https://docs.trdr.io/key-features-and-indicators/volume-indicators/cumulative-volume-delta-cvd)). |
| Detection | Session-reset CVD; bearish: HH price, LH CVD. |
| Compare | Spot vs perp CVD when possible. |

### 26. `session_killzone`

| Field | Detail |
|-------|--------|
| Level | B |
| Windows (EST) | Asian build 20:00–00:00; London 02:00–05:00; NY 07:00–10:00 ([ictkillzone](https://www.ictkillzone.com/ict-kill-zones), [hornx](https://hornx.trading/en/blog/sessions-killzones-trading)). |
| Detection | In window; Asian range; sweep; break with momentum. |
| Anti | Weekend; outside window. |

### 27. `wyckoff_spring`

| Field | Detail |
|-------|--------|
| Level | A |
| Concept | Spring below TR support, recover ([Wyckoff Analytics](https://www.wyckoffanalytics.com/wyckoff-method/), [Quantum accumulation](https://www.quantum-algo.com/blog/wyckoff-accumulation-trading-guide/)). |
| Entry | After SOS bar; SL below spring low. |

---

## Microstructure

### 28. `depth_imbalance`

| Field | Detail |
|-------|--------|
| Level | C→B |
| Concept | OBI + microprice ([fxsi](https://fxsi.com/depth-imbalance-and-micro-price-efficiency/), [CryptoStats](https://docs.cryptostats.dev/streaming/orderbook-dynamics.md)). |
| Detection | \|OBI\| > 0.33; microprice align; 15m bar aggregate. |
| ACTION | Only with structure; stale book → WATCH. |

### 29. `whale_walls`

| Field | Detail |
|-------|--------|
| Level | C→B |
| Detection | Wall > k× median size; rejection wick; top-50 liquidity. |

### 30. `spread_strategy`

| Field | Detail |
|-------|--------|
| Level | C→B |
| Detection | Spread < p20 → expansion ROC; flag in reasons on 15m. |

### 31. `absorption`

| Field | Detail |
|-------|--------|
| Level | A |
| Concept | High delta, flat price ([TradeZella](https://www.tradezella.com/learning-items/footprint-charts), [Emoji](https://www.emojitrading.com/docs/order-flow-basics/key-order-flow-traded-volume-concepts/absorption/)). |
| Detection | \|delta\| high percentile; range < 0.3× ATR at level. |

### 32. `aggression_shift`

| Field | Detail |
|-------|--------|
| Level | B |
| Data | aggTrade + `takerlongshortRatio`. |
| Detection | Taker buy/sell dominance flip over 5–15m + price break. |

---

## Positioning / liquidity

### 33. `oi_divergence`

| Field | Detail |
|-------|--------|
| Level | A |
| Concept | OI↑ price↓ bearish build; OI↓ price↑ short covering ([Axel Adler](https://axeladlerjr.com/bitcoin-open-interest-price-divergence-patterns/), [Blackperp](https://blackperp.com/academy/what-is-open-interest-divergence)). |
| Threshold | 7d OI Δ > ±15% vs price Δ ≥ 5% opposite. |

### 34. `ls_ratio_extreme`

| Field | Detail |
|-------|--------|
| Level | B |
| Concept | Contrarian fade ([Kalena L/S](https://blog.kalena.ai/long-short-ratio-in-crypto-the-signal-most-traders-read-backwards)). |
| Detection | global ratio > 2.5 or < 0.4 + reversal bar + funding align. |

### 35. `liquidation_heatmap`

| Field | Detail |
|-------|--------|
| Level | B |
| Data | `!forceOrder@arr`, OI drop proxy, wick reclaim. |
| Concept | Fade cascade after liq spike. |

### 36. `stop_hunt_detection`

| Field | Detail |
|-------|--------|
| Level | A |
| Concept | Equal highs/lows cluster sweep + reclaim (explicit pool). |

---

## Cross-asset

### 37. `btc_correlation`

| Field | Detail |
|-------|--------|
| Level | B |
| Concept | Alt ROC aligned with BTC bias; skip BTCUSDT. |
| Tier | Usually WATCH unless full LTF plan. |

### 38. `altcoin_season_index`

| Field | Detail |
|-------|--------|
| Level | B |
| Concept | % top alts outperform BTC 90d → 0–100 ([CMC](https://coinmarketcap.com/charts/altcoin-season-index/), [Markets Unplugged](https://www.themarketsunplugged.com/altcoin-season-index-2026/)). |
| Trade | ASI 40→50 early WATCH; 75+ late; <25 bitcoin season penalty for alt longs. |
| Proxy | Compute from public ticker24h basket. |

---

## Tier matrix (WATCH vs ACTION)

| Family | WATCH minimum | ACTION extra |
|--------|---------------|--------------|
| SMC | Pattern + HTF bias | + killzone or volume; R:R ≥ 1.5 |
| Trend | HTF + LTF trigger | + volume; funding not against |
| Breakout | Squeeze/break seen | + volume; ADX not chop |
| Reversal | Extreme + level | + confirm bar; OI/funding 2nd factor |
| Micro | Metric extreme | + structure; not micro-only |
| Positioning | Divergence/extreme | + price confirm; perp |
| Cross | Regime flag | + per-symbol LTF plan |

---

## Calibration appendix (from web, not bot2)

| Parameter | Suggested start | Source hint |
|-----------|-----------------|-------------|
| funding z-score window | 60d | Adeline117 / quant |
| funding z entry | ±2.5 |同上 |
| volume_ratio confirm | ≥ 1.2–1.5× MA20 | Tapbit, VWAP guides |
| BB squeeze width p | < 10th percentile | QuantifiedStrategies |
| OI divergence 7d | ±15% OI vs ±5% price | Axel Adler |
| L/S ratio extreme | > 2.5 / < 0.4 | PerpFinder/Kalena |
| OBI threshold | ±0.33 | depth_imbalance literature |
| Turtle lookback | 20 bars 1h | Raschke / Orbex |
| ST ATR / mult | 10–14 / 3–4 | Crypto trading guide |
| RSI hidden pullback | long RSI < 58 | multi_tf / hidden div |
| Min ADX 1h | 15–18 | structure_pullback |
| Impulse ROC10 15m | 0.15–0.5% | price_velocity |
| ASI early rotation | cross 40→50 | Markets Unplugged |

---

## Live verification checklist

Per `setup_id`:

1. Concept cited (2+ URLs in this doc).
2. Required Binance endpoints respond (`PYTEST_LIVE=1`).
3. Dry-run telemetry: pattern fires on BTC + one alt over 24h paper run.
4. Document false-positive rate after 7d paper track.

---

## Future catalog (§7A backlog)

Not in the 38: `basis_zscore`, `taker_ratio_extreme`, `risk_on_off_banner`, `htf_structure_break` (1h trigger), `swing_smc_ob` (4h). See plan §7A.
