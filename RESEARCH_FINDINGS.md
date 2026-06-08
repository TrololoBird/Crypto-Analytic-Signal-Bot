# RESEARCH FINDINGS — Strategy & Architecture (web research, 2026-06-08)

Внешний ресёрч по литературе/GitHub/статьям для усовершенствования сигнального бота.
Round 1 зафиксирован. Round 2 в процессе.

---

## ROUND 1 — выводы

### Таймфреймы по стратегиям
Индустриальный консенсус: **HTF bias (4h) → confirmation (1h) → entry refine (5m/15m)**, ratio 4:1.
Жёсткая привязка к 15m — ошибка. `config.toml` сейчас `kline_intervals=['15m']`.

| Класс | Trigger TF | HTF bias | Entry refine |
|---|---|---|---|
| SMC структурные (order_block, fvg, bos_choch, breaker_block, liquidity_sweep, structure_*) | 1h | 4h | 5m/15m |
| Reversal/divergence (cvd_divergence, indicator_divergence, hidden_divergence, rsi_divergence) | 15m | 1h | 5m |
| Flow/sentiment (funding_reversal, ls_ratio_extreme, oi_divergence) | 1h–4h | — | 15m |
| Liquidation/orderflow (liquidation_heatmap, absorption, depth_imbalance, whale_walls) | 5m–15m | 1h | 1m |
| Volatility/momentum (squeeze, keltner, atr_expansion, supertrend, volume_anomaly) | 15m | 1h | 5m |
| Scalp/microstructure (price_velocity, aggression_shift, orderflow_imbalance) | 5m | 15m | 1m |

### Подтверждённые баги (research → код)
- **A. Stale pattern**: «Signal locks the moment a candle closes and never move». intra_candle_scanner перегенерирует паттерн каждый цикл. Фикс: lock на close + дедуп до следующего close.
- **B. FVG/OB mitigation не отслеживается**: «Never trade a mitigated FVG — one-time use zone». fvg.py/order_block.py не проверяют использование зоны.
- **C. liquidation_heatmap направление**: «cluster swept → magnet disappears → price reverses». Это sweep-and-reverse, вход ПОСЛЕ свипа (разворот), не в кластер.
- **D. Фиксированные пороги**: entry_pad_atr_mult=0.35 и chase_pct=0.8% должны быть volatility-adaptive (f(ATR percentile)).

### Ответы на вопросы
- **CVD**: lookback 21 бар. Bullish div = price LL + CVD HL. CVD как final gate после regime+BOS+OTE, не первичный триггер.
- **OB strength** = min(highVol,lowVol)/max(highVol,lowVol). FVG fill ~70%. Mitigation by close OR high/low (параметр close_mitigation).
- **Confluence**: buy-сигналы нужна высокая precision. 3-of-5 разумно. crowd_position часто фейлит — норма (контрарность).
- **Kelly/SL**: n=44 СТАТИСТИЧЕСКИ НЕДОСТОВЕРЕН. Нужно 50+ (лучше 100+). 55% win на 100 trades = CI ±10%. Quarter Kelly + crypto tail risk → стресс 2× downside.
- **Режим**: bear+btc_uptrend divergence нормален (alts≠BTC). ADX>25 trend, <20 range, ATR percentile volatile. Volatile = wider stops, не блок.
- **Binance API неиспользуемое**: /futures/data/topLongShortAccountRatio (top trader L/S), /fapi/v1/allForceOrders (ликвидации 7д), /futures/data/takerlongshortRatio.
- **Funding**: Binance cap ±0.3%/interval, dampening ±0.05%, settle 1h/4h/8h (с 2026-01 адаптивно). Funding arbitrage 2025: ~19% годовых, DD<2%.
- **L/S ratio**: top 20% по марже = top trader ratio. Extreme = contrarian warning.

### Приоритеты
| P | Действие |
|---|---|
| P0 | Мульти-ТФ kline_intervals + per-strategy trigger_tf |
| P0 | Сигнал-лок на close + дедуп |
| P1 | FVG/OB mitigation tracking |
| P1 | liquidation_heatmap sweep-and-reverse |
| P1 | volatility-adaptive chase_pct |
| P2 | 100+ outcomes до выводов; Quarter Kelly |
| P3 | top_trader L/S endpoint |

### Sources Round 1
- github.com/joshyattridge/smart-money-concepts
- bookmap.com CVD, litefinance.org FVG
- developers.binance.com L/S & OI API
- altrady.com Kelly crypto
- coinglass.com Liquidation
- oboe.com SMC MTF, quantmonitor.net regime

---

## ROUND 2 — детали по стратегиям

### Order Block mitigation (точный алгоритм)
- Два метода: **Wick** (mitigate если фитиль вышел за OB) vs **Close** (mitigate если close вышел за OB).
- SL ставится ЗА фитилём OB, никогда внутри тела. Если фитиль свипнут — institutional thesis invalid.
- joshyattridge lib: параметр `close_mitigation` (bool).
- **Наш fvg.py/order_block.py**: добавить tracking mitigated zones + параметр close vs wick.

### Liquidity Sweep / Stop Hunt (точная логика)
- Детект: price пробивает swing high/low, НЕ удерживает, **закрывается обратно в range**. Failed breakout = сигнал, НЕ breakout.
- Confirmation: ждать слом внутренней структуры (MSS) на младшем ТФ для подтверждения разворота.
- Delta анализ внутри фитилей wick для идентификации hunt-зон.
- **Наш liquidity_sweep.py / stop_hunt_detection.py**: вход = разворот ПОСЛЕ свипа + MSS confirmation на LTF.

### Wyckoff Spring/UTAD (точная логика)
- **Spring** (accumulation): ложный пробой ПОД support + быстрый возврат, на **низком объёме** = buy. Ловит шорты.
- **UTAD** (distribution): ложный пробой НАД resistance, свипает buy-side liquidity, на низком объёме = sell.
- Объём ОБЯЗАТЕЛЕН: падает на тестах S/R, растёт на SOS breakout.
- **Наш wyckoff_spring.py**: проверить объёмное подтверждение (low volume на пробое = истинный spring).

### OI Divergence (точная интерпретация)
- Price ↑ + OI ↓ = bearish (rally на short-covering, без новых longs → fade).
- Price ↓ + OI ↑ = shorts building (часто = дно близко).
- OI+price together = сильный тренд; divergence = ослабление.
- «Самые мощные сделки — inversions: OI падает, price в обратную сторону = squeeze».
- **Наш oi_divergence.py**: market-order reversal, проверить direction logic.

### VWAP Trend (deviation bands)
- Session-anchored VWAP, рестарт каждую сессию.
- Bands: ±1σ = 68% (value area, balanced), ±2σ = 95% (extreme), ±3σ = 99.7% (black swan = лучшие counter-trend mean-reversion).
- Entry: price trending над/под VWAP → вход на возврате к VWAP линии + volume confirmation. TP = prior swing / 1st deviation band.
- **Наш vwap_trend.py**: limit-order на возврате к VWAP — корректно зонная стратегия.

### Absorption / Iceberg / Depth Imbalance
- Iceberg: level поглощает больше объёма чем показано в book (book 5 BTC, прошло 23 BTC без движения цены).
- Depth imbalance: ratio bid/ask depth в 0.5% от mid; сдвиг 1.5:1 → 0.6:1 за 30s = смена sentiment.
- Absorption: подтверждать через CVD / aggressive trade imbalance.
- **Наш absorption.py / depth_imbalance.py**: нужны L2 depth + CVD confirmation, 0.5% band, 30s window.

### Risk/Reward (breakeven win rate)
- Минимум 1:2, лучшие сетапы 1:3. Crypto day: min 2:1.
- Breakeven (net с costs): 1:1 → 51%, 1:2 → 36%, 1:3 → 27%.
- 40% win @ 1:3 > 80% win @ 1:0.5. RR важнее win rate.
- **Наш min_rr=1.9** — близко к 1:2, корректно. Можно поднять до 2.0 для чистоты.

### Overfitting (КРИТИЧНО для наших стратегий)
- **90% crypto стратегий переоптимизированы и фейлят live**.
- Red flags: win rate >80%, работает только на 1 coin/TF, >6-8 параметров, equity без drawdown, большой gap backtest vs live.
- Walk-forward: optimize 6мес → test 2мес unseen → repeat. Склеить только OOS сегменты.
- Out-of-sample: optimize 70%, validate 30% untouched. Collapse на OOS = overfit.
- **Наш 100% SL rate live** — возможный признак overfit ИЛИ малая выборка (n=44). Нужен walk-forward + OOS.

### Backtest caveat
- OHLCV пропускает intra-candle движение, bid-ask spread, depth → занижает slippage, fills выглядят лучше реальных.

### Приоритеты Round 2 (дополнение)
| P | Действие |
|---|---|
| P1 | OB/FVG: close vs wick mitigation параметр + tracking |
| P1 | liquidity_sweep/stop_hunt: failed-breakout + MSS confirmation на LTF |
| P1 | wyckoff: low-volume confirmation на пробое |
| P2 | absorption/depth: L2 + CVD confirm, 0.5% band, 30s window |
| P2 | min_rr 1.9 → 2.0 |
| P2 | Walk-forward + OOS валидация всех стратегий (anti-overfit) |
| P3 | VWAP ±3σ как mean-reversion counter-trend сигнал |

### Sources Round 2
- in.tradingview.com/scripts/orderblocks, quantum-algo.com OB guide
- mql5.com SMC Liquidity Sweep, zeiierman.com sweeps vs stop hunts
- margex.com Wyckoff, chartmini.com Wyckoff, quantum-algo.com Wyckoff distribution
- sharpe.ai OI, tradingview OI divergence
- academy.exmon.pro VWAP, orderflowlabs.com VWAP
- bookmap.com iceberg, quantstrategy.io order book imbalance, buildix.trade orderflow
- trendrider.net overfitting, surmount.ai walk-forward, arxiv 2209.05559 DRL backtest overfit
- metrotrade.com RR ratio, clearank.com breakeven win rate

