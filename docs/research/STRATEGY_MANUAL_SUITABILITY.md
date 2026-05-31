# Пригодность стратегий для signal-only (ручной вход)

**Не про «подкрутить пороги».** Часть детекторов по своей природе рассчитана на **авто-исполнение** или HFT-горизонт: сигнал устаревает за секунды–минуты, нет полного trade plan, или это чистый **индикаторный алерт** как у [Crypto-Signal](https://github.com/CryptoSignal/Crypto-Signal).

Целевой продукт: **trade plan** (зона entry, SL, TP, invalidation) + время на реакцию подписчика ([United Kings execution](https://unitedkings.net/how-to-execute-forex-signals-like-a-professional-trader-10/), [Mudrex format](https://mudrex.com/learn/best-crypto-signal-providers-on-telegram/)).

## 1. Критерии (не пороги score)

| Критерий | Manual-suitable | Auto-oriented |
|----------|-----------------|---------------|
| **Latency budget** | Подписчик успевает за **2–15 мин** (прочитать + bracket) | Edge исчезает за **&lt;60 с** |
| **Trigger** | Close **15m+** (иногда 1h) | 1m/5m/intrabar/book tick |
| **Output** | Zone + SL + TP + invalidation | «RSI hot», «depth skew» без уровней |
| **Смысл** | Setup с location (structure/liquidity) | Сканер «все монеты × все индикаторы» |
| **Роль в канале** | ACTION или WATCH→ACTION | Только WATCH, macro banner, или **redesign** |

### Классы в каталоге

| Class | ACTION в TG | Что делать в коде/spec |
|-------|-------------|-------------------------|
| **M1** Manual-primary | Да, основной поток | Оставить; trigger ≥15m |
| **M2** Manual-conditional | Да, после confirm bar + structure | Ужесточить детектор, не только score |
| **W** WATCH-only | Silent radar | Не строить полный plan как ACTION |
| **C** Context | Regime / BTC banner | Влияет на **другие** setup, не solo ACTION |
| **R** Redesign / disable ACTION | Нет (пока) | Переписать под 15m aggregate или убрать из ACTION |

## 2. Матрица 38 setup_id

| setup_id | Class | Почему | Рекомендация |
|----------|-------|--------|--------------|
| structure_pullback | M1 | 15m pullback + HTF, зона у structure | ACTION |
| structure_break_retest | M1 | 1h break, 15m retest — время на limit | ACTION |
| wick_trap_reversal | M1 | Close 15m после raid | ACTION |
| squeeze_setup | M1 | Compression → break на close | ACTION |
| ema_bounce | M1 | Классический LTF pullback | ACTION |
| fvg_setup | M1 | CE limit в зоне после close | ACTION |
| order_block | M1 | Retest OB после BOS | ACTION |
| liquidity_sweep | M1 | Sweep + reclaim на close | ACTION |
| bos_choch | **C/W** | Часто **фильтр** направления, не solo entry | WATCH или filter; ACTION только с OB/FVG |
| hidden_divergence | M1 | Continuation, 15m+ | ACTION |
| indicator_divergence | M2 | Нужен уровень + 2-of-4 | ACTION если structure confirm |
| funding_reversal | M2 | 1h extreme, 15m reversal bar | ACTION |
| cvd_divergence | M2 | 15m session CVD, не тик | ACTION |
| session_killzone | M1 | Clock + range — человек готовит лимиты | ACTION |
| breaker_block | M1 | SMC retest | ACTION |
| turtle_soup | M1 | 1h false break | ACTION |
| vwap_trend | M1 | Session reclaim 15m | ACTION |
| supertrend_follow | M1 | Pullback к ST | ACTION |
| price_velocity | **R** | Импульс 5m — **слишком быстро** для ручного | WATCH «impulse» или disable ACTION |
| volume_anomaly | **W** | Spike без setup = scanner alert | WATCH; ACTION только + structure break |
| volume_climax_reversal | M1 | Climax bar 15m | ACTION |
| keltner_breakout | M1 | Break + retest | ACTION |
| whale_walls | **R** | Book меняется за секунды | WATCH at wall; no standalone ACTION |
| spread_strategy | **R** | bookTicker HFT | Context flag only |
| depth_imbalance | **R** | OBI sub-minute | 15m aggregate → M2 или WATCH only |
| absorption | M2 | Нужен уровень + next bar confirm | ACTION |
| aggression_shift | M2 | 15m taker flip + structure | ACTION |
| liquidation_heatmap | M2 | Wick reclaim 15m | ACTION |
| stop_hunt_detection | M1 | Raid + reclaim | ACTION |
| multi_tf_trend | M1 | HTF alignment | ACTION |
| rsi_divergence_bottom | M1 | Regular div + support | ACTION |
| wyckoff_spring | M1 | 1h TR spring | ACTION |
| bb_squeeze | M1 | Squeeze release | ACTION |
| atr_expansion | M2 | Часто news spike — calendar filter | ACTION с filter |
| ls_ratio_extreme | M2 | 4h extreme, 15m confirm | ACTION |
| oi_divergence | M2 | 4h OI, 15m entry | ACTION |
| btc_correlation | **C** | Alignment flag для alts | WATCH; не ACTION на «corr only» |
| altcoin_season_index | **C** | Daily breadth | Digest / WATCH; не intraday ACTION |

**Итого (38 setup_id):**

| Class | Count | Доля ACTION-потока `[target]` |
|-------|------:|-------------------|
| M1 | 21 | основной ACTION |
| M2 | 9 | после confirm |
| W/C | 4 | 0% solo ACTION (bos → filter/context; volume_anomaly → WATCH) |
| R | 4 | 0% ACTION до redesign |

**`[spec]`:** R-class **не** публикует solo ACTION до redesign; enforcement в config + delivery policy (большое изменение).

## 3. Почему это не «пороги»

Пример: `depth_imbalance` с `score ≥ 0.75` всё равно **бесполезен** как ACTION — к моменту открытия TG книга уже другая. Нужно:

1. **Переписать детектор** — агрегат за 15m bar, не snapshot.
2. Или **понизить роль** — только confluence factor для M1-setup.
3. Или **отключить ACTION** в `config_strategies.toml` (`action_enabled: false`).

То же для `price_velocity`, `spread_strategy`, `whale_walls`.

## 4. Сравнение с Crypto-Signal (почему «много сигналов»)

См. [CRYPTO_SIGNAL_COMPARISON.md](CRYPTO_SIGNAL_COMPARISON.md).

Кратко: Crypto-Signal — **indicator scanner**, не trade-plan bot. `update_interval: 300`, сотни пар, `alert_frequency: always` на 5m RSI → лавина сообщений **без SL/TP**. Для ручного канала это антипаттерн; берём **модульность индикаторов**, не модель «500 монет × always alert».

## 5. Целевые правила продукта (вместо Crypto-Signal volume)

| Правило | Значение |
|---------|----------|
| Shortlist | 40–55 + **7 anchors** always |
| Детекторы на символ | 8–15 families, не 38 на каждый tick |
| ACTION | 15–40/day, burst cap |
| R-class | No ACTION until redesign |
| Publish | Candle **close** only |

## 6. Roadmap стратегий (spec)

1. **Wave 1:** все **M1** — проверить trade plan + 15m trigger.  
2. **Wave 2:** **M2** — добавить confirm bar в `detect()`, не score.  
3. **Wave 3:** **R** — aggregate или WATCH-only.  
4. **Wave 4:** **C** — macro service, не strategy plugin ACTION.

Связь: [STRATEGY_CATALOG.md](STRATEGY_CATALOG.md), [SIGNAL_ONLY_PRODUCT.md](SIGNAL_ONLY_PRODUCT.md), [SIGNAL_EVALUATION.md](SIGNAL_EVALUATION.md).
