# 50 вопросов внешнему аналитику: ответы и рекомендации

**Легенда:** 🟢 факт/источник · 🟡 консенсус практики · 🔴 только ваши данные
**Контекст бота:** signal-only, Binance USD-M, ~28 стратегий, delivery через score+MTF+confluence 3-of-5.

> Цены BTC 59k–67k — исторические; принципы структуры/уровней TF-агностичны и применимы к любому режиму.

---

## I. Рыночная рамка и MTF (1–10)

**1. 4H-даунтренд vs 1H-контртрендовый отскок**
🟡 4H-даунтренд: последовательность lower-lows + lower-highs на дневных/4H-барах, цена ниже EMA20/EMA50 4H. 1H-отскок: локальные higher-highs на 1H без слома 4H-структуры (нет BOS вверх на 4H). Формальный тест: 4H-бар ещё не закрыл новый HH выше последнего 4H-пика → отскок, не разворот.

**2. Признаки, что 1H-отскок «жив»**
🟡 (a) 1H close выше последнего 1H swing-high; (b) CVD растёт вместе с ценой; (c) объём выше на up-bars; (d) нет нового 1H lower-low; (e) 15M импульс без divergence. Все 5 → отскок активен.

**3. Доминирующий горизонт для шортов на альтах**
🟡 Bias = 4H. Тайминг = 1H (ждать 1H-сигнала окончания отскока). BTC — дополнительный макро-якорь. Шортовать альт при 4H-медвежьем BTC + 4H-медвежьем альте + 1H-завершение отскока (rejection, не bounce base).

**4. Ключевые уровни как hard-gate для шортов/лонгов**
🟡 Уровни работают как контекстные ворота, а не как жёсткие числа: short-bias — пока цена под ближайшим swing-высоким 4H; long-gate открывается при закрытии 4H выше него. Абсолютные числа (59k/64k) устаревают — используй относительную структуру.

**5. Когда 15M-сжатие = «не входить вообще»**
🟡 Если ADX<20 + ATR сжат (внизу 20-перц за 50 баров) + BB/Keltner сужены → рынок в нерешённости. Направление по HTF не помогает: любой вход = coin-flip до расширения.

**6. ADX/ATR порог «диапазон vs тренд» на 1H**
🟡 ADX<20 = range (Wilder). ADX>25 = тренд. Зона 20–25 = нейтральная. ATR-дополнение: ATR(текущий) / ATR(50-period avg) < 0.8 → поджатие, skip.

**7. 3–5 признаков подтверждения sweep**
🟡 (a) Wick ниже уровня с быстрым reclaim (close выше); (b) spike объёма на sweep-баре; (c) CVD резко вниз на sweep + восстановление; (d) absorption (объём не двигает цену дальше вниз); (e) BOS вверх на 15M после reclaim.

**8. TTL сетапа после sweep+reclaim**
🟡 [√t] В минутах: ~2–5 баров entry-TF. На 15M → ~30–75 мин; на 1H → ~2–5ч. После этого "вход = погоня". Лучше параметром `ttl_minutes`.

**9. Distribution vs Accumulation (Wyckoff)**
🟡 Distribution: распределение на хаях (BC/AR/ST→SOW→LPSY); объём на up-barах убывает, HH без объёма. Accumulation: spring/shake-out + объём поглощения + PS→SC→AR→ST→SOS. −50% от ATH ≠ accumulation само по себе; нужен фаза-B (chop) + spring.

**10. Должен ли бот шортить BTC в discount**
🟡 В discount-зоне (ниже 50% торгового диапазона) short BTC — против natural-order (BTC ищет liquidity снизу). Разумнее: шортить только при явном слабом ралли с rejection от POI, только если 4H + 1H оба bearish. Альты с относительной слабостью — более логичная цель.

---

## II. CVD / orderflow (11–18)

**11. CVD bearish div при neutral 1H — шорт ок?**
🟡 Без RSI≥60–70 дивергенция на нейтральном 1H — слабый сигнал. Рекомендуется: CVD div + RSI≥60 + rejection wick + отсутствие 1H нового HH. Один CVD-div = confluence-leg, не самостоятельный триггер.

**12. Минимальный delta-shift для edge**
🔴 Публичного σ-порога нет. Практика: delta-shift ≥ 1.5–2σ скользящего окна последних N баров. Калибруется только на вашей телеметрии.

**13. CVD-short в ranging ADX~25 vs даунтренд**
🔴/🟡 В ranging win-rate заведомо хуже (нет «natural direction»). Качественная оценка: в trending win-rate ~35–45%, в ranging ~20–30% для CVD-short. Точные цифры — телеметрия.

**14. Session CVD exhaustion vs bar-delta divergence**
🟡 Session exhaustion (накопление по сессии) → для разворотных позиций на HTF (1H/4H). Bar-delta → для intrabar входов (15M). Оба дают ложняки в choppy range; filter: session exhaust только при ADX>20 + HTF bearish bias.

**15. Нужно ли подтверждение (bearish close) для CVD-short?**
🟡 Да. Limit на HH без close-confirmation → высокий риск HH-extension. Bearish close под HH = confirmed rejection. Снижает количество сигналов но улучшает качество. Рекомендую: require_close_confirmation=True.

**16. SL для CVD-short на мемкоине (ATR~1%)**
🟢 [ATR-SL источники] Для volatile crypto-swap: min 1.5–2×ATR. При ATR=1% → SL = 1.5–2% от entry. За HH+buffer = HH + 0.3–0.5×ATR. Текущий min_stop_normalized=1.1% = ~1.18×ATR = **внутри noise band → слишком tight.**

**17. TP: prior-segment vs фиксированный 2R**
🟡 В 4H bear + 1H bounce: prior-segment лучше (структурная цель адаптируется к реальному диапазону). Фиксированный 2R работает хуже, если 1H-bounce сокращает движение. Для ranging → структурная цель; для trending → фиксированный R.

**18. 7/7 delivery = short cvd_divergence — режим или перекос?**
🟡/🔴 Скорее всего **и то и другое**: медвежий режим создаёт CVD-дивергенции (рыночное смещение); но если все 7 дали SL → детектор не фильтрует bounce-фазу. Диагноз: проверить, был ли htf_conflict=False на всех 7 → если да, фильтр пропустил bounce.

---

## III. Стопы, входы, SL-аналитика (19–26)

**19. MFE=0%, сразу SL (FARTCOIN): что из трёх?**
🟡 Скорее всего **комбинация: поздний вход + tight stop**. MFE=0 = рынок не пошёл в вашу сторону ни на мгновение → либо direction wrong (вошли в момент reversal), либо вход уже после движения (chase). Tight stop → нет запаса на adverse excursion. Диагностика: сравнить `activation_at` с `signal_published_at` (lag) и `SL_distance / ATR`.

**20. Макс. adverse move для limit без «chase»**
🟡 Стандарт: активация в пределах ≤0.3×ATR от уровня сигнала → допустимо. >0.5×ATR → chase. 0.1188 при сигнале 0.1190 = 0.17% = при ATR 0.93% → 0.18×ATR → **на грани, ок.**

**21. Staleness 1.5×ATR: мягко или жёстко для volatile альтов?**
🟡 Для volatile альтов (ATR>0.5%/bar) 1.5×ATR — **мягковато** (зона быстро протухает). Рекомендуется 1.0–1.2×ATR. Для медленных активов — нормально.

**22. Fill 0.1188 при сигнале 0.1190 — нормально?**
🟡 0.17% отклонение = ~0.18×ATR(0.93%) → в пределах допуска для limit-fill. Не late chase. Нормально.

**23. SL 1.1% на мемкоине с ATR 0.93%**
🟢 SL = 1.1% / ATR = 0.93% → **1.18×ATR = внутри noise band** (шум 1-2 баров). Рекомендованный floor для volatile crypto: **1.5–2×ATR = 1.4–1.86%**. Текущий min_stop недостаточен.

**24. MAE/MFE профиль «хорошего» шорта в 4H bear**
🟡 Первые 15–30 мин: MAE малый (≤0.3×ATR), MFE начинает расти сразу. Если за 1H нет MFE>0.5×ATR — setup под сомнением. Профиль: быстро в прибыль, медленно вниз.

**25. Сколько R нужен средний winner при hit-rate 25%?**
🟢 Математика ожидания: E = (WR × avg_win) − (LR × avg_loss) ≥ 0. При 25% WR и avg_loss=1R: 0.25×R − 0.75×1 ≥ 0 → **avg_winner ≥ 3R для break-even.** Для позитивного expectancy → >3R.

**26. 3 причины SL в таких ботах — распределение**
🟡 Консенсусная оценка (нет строгих данных): ~40% timing (поздний вход/chase), ~35% regime mismatch (шорт в bounce, лонг в breakdown), ~25% stop placement (inside noise band). Разделить на практике: тег `cause_of_sl` в телеметрии.

---

## IV. Delivery, фильтры, confluence (27–34)

**27. min_score=0.65 при hit-rate 25%: высокий/низкий/ок?**
🟡 Если delivery ~1 из 100+ hits — фильтр очень строгий. 0.65 при таком ratio может убивать валидные сетапы ради false-positive reduction. Рекомендуется: снизить к 0.60 + контролировать quality через `htf_conflict` и `pd_zone_mismatch`, а не score-порогом.

**28. 5 must-have confluence для шорта в 4H bear + bounce 1H**
🟡 (a) 4H bearish структура (новый 4H LL или rejection от 4H POI); (b) BTC 4H = downtrend; (c) 1H отскок завершён (rejection+bearish close 1H); (d) CVD bearish div или OB-rejection; (e) вход NOT в discount-zone (или структурный SOS отсутствует).

**29. htf_conflict / htf_reversal_conflict: когда резать, когда ложная отсечка?**
🟡 Резать правильно: 1H momentum свежий (новый HH + объём + CVD) → шорт premature. Ложная отсечка: 1H «отскок» = один бар noise, 4H структура явно медвежья → conflict overrules valid short. Решение: задать `htf_reversal_conflict` только при 1H-BOS вверх + minimum 2 HH.

**30. short_blocked без 4H_downtrend: достаточно ли?**
🟡 Нет. В bounce-фазе 4H структура может ещё быть технически «не подтверждённым даунтрендом» (первая волна вниз). Нужен дополнительный gate: `1h_bull_momentum` → block short. Иначе бот шортит в основании отскока.

**31. score_too_low убивает валидные сетапы: что именно**
🟡 Чаще всего «убивают»: (a) pd_zone_penalty (в discount → score−0.2); (b) ATR-expansion filter (низкий ATR = low score); (c) HTF-конфликт penalty. Adjustments: (a) сделать discount = soft penalty (−0.1), не −0.2; (b) снизить ATR-порог; (c) review htf-weights.

**32. pd_zone_mismatch_short_in_discount: блокировать всегда?**
🟡 Да, **hard-block** для signal-only бота без структурного override. Short в discount = против natural order; риск sweep вниз → reclaim → long. Исключение только при явном distribution после Wyckoff-phase-B.

**33. ADX hard gate ranging market downgrade: помогает или пропускает?**
🟡 Двойственно: помогает избежать chop. Но ADX — lagging; при начале даунтренда ADX < 20 ещё несколько баров → пропускает именно начало движения. Рекомендуется: ADX как мягкий weight-modifier (−0.1 при ADX<20), не hard-gate.

**34. ema_bounce + keltner_breakout как confluence: усиливает или дублирует?**
🟡 Дублирует. Оба = «цена у динамического уровня + momentum». Они описывают один тезис двумя индикаторами из одного семейства (price-channel/MA-based). В score их суммировать нельзя — считать как один trend-leg.

---

## V. Режим и BTC-якорь (35–40)

**35. 4H bear + composite bull: какой label показывать**
🟡 Показывать: `CONFLICTED (BEAR_BIAS)`. Торговать: suspend shorts до разрешения (1H rejection или new 4H LL). Не «suspended all» — bias медвежий, ждать подтверждения.

**36. В 4H bear + 1H bounce: смещаться к лонгам?**
🟡 Частично да. Relative-strength альты (растут при BTC flat или вниз) → long с уменьшенным size + более высокий score-threshold. Не full bias shift — скорее «short-bias reduced, long-opportunity allowed».

**37. barrier_short: при каком % BTC за N мин оправдан?**
🟡 Ориентир: BTC +1–2% за 15–30 мин против открытых шортов → trigger. Не одно событие, а price × velocity. Точный порог — эмпирически на вашем historical data.

**38. Emergency exit vs natural SL: когда досрочно?**
🟡 Досрочно: при смене режима (BTC reclaim ключевого 4H-уровня + 15M bullish BOS), не просто при движении цены. Natural SL — при одиночном adverse move без смены структуры. Emergency = structural flip.

**39. Торговать ли BTC short в диапазоне mid-range?**
🟡 Большинство практиков: **нет** при conflicted HTF (4H не подтверждён, 1H bounce). Если торговать — только с hard stop за ближайший 4H HH и минимальным size. Без кода — риск = reward ≤1.

**40. Altcoin season ~67% при BTC downtrend**
🟡 Конфликт. Высокий alt-season = money flow в альты → шортить альты сложнее. Лучший вариант: long relative-strength алты, не short weak-alts. Если шортить — только альты с явной индивидуальной слабостью vs BTC.

---

## VI. Стратегии и пороги (41–46)

**41. Приоритетные стратегии в 4H bear + 1H bounce**
🟡 **Приоритет:** (a) `structure_break_retest` long на локальном откате (bounce-trade с 4H-контекстом); (b) `cvd_divergence` short только после 1H rejection; (c) `liquidity_sweep` long при sweep под key low + reclaim. **Выключить:** `volume_anomaly` short (chase в compression); `squeeze_setup` short (до расширения).

**42. liquidity_sweep не ловит sweep под уровнем**
🟡 Проверить в первую очередь: (a) `volume_ratio_threshold` — возможно слишком высокий (повысить ≥1.5× MA → снизить до ≥1.2×); (b) `lookback_bars` — слишком короткий, уровень вне lookback; (c) `wick_body_ratio` — wick не достаточно длинный по правилу.

**43. volume_anomaly short на BTC в сжатии**
🟡 **Mean-reversion trap** с высокой вероятностью. Объём-аномалия в сжатии чаще = ложный breakout или поглощение. Short в сжатии против volume-spike = ловить нож. Disable в compression-режиме.

**44. structure_pullback long при 4H down: когда оправдан?**
🟡 Оправдан при: (a) 1H структура bullish (HH+HL цепочка); (b) confluenced entry (CVD + OB/FVG на 1H); (c) BTC нейтральный или ± на 1H; (d) score ≥ 0.68 с htf_conflict=False. Как INJ +4.4% — это relative-strength long в bear market, не общий разворот.

**45. indicator_divergence long vs cvd_divergence short на одном рынке**
🟡 Условие разведения: (a) не публиковать оба одновременно — первый заблокировать при появлении второго; (b) приоритет = тот, что согласован с HTF bias (при 4H bear → cvd_short доминирует над div_long); (c) если оба = конфликт → не публиковать ни одного.

**46. Целевой mix long/short delivery в 4H bear + bounce 1H**
🟡 ~40–50% short / 50–60% long. Не 70/30 short (шортить против bounce = высокий SL rate). Не 30/70 long (4H контекст = bear, не разворот). Середина.

---

## VII. Операционка и валидация (47–50)

**47. N для калибровки одной стратегии**
🟢 [источники по sample size] Min = **100 activated outcomes** для базовой статистической значимости (±10% доверительный интервал win-rate при 95% CL). Для надёжной калибровки — **200+ trades per regime** (не смешивать bull/bear outcomes). 50 trades = гипотеза, не вывод.

**48. 5 еженедельных метрик оператору**
🟡 (a) SL_rate разбитый по режиму (4H bear / neutral / bull); (b) expired_rate по TF и стратегии; (c) MFE_max до SL (MFE=0 = timing/direction error); (d) time_to_exit (минуты): median и 90th percentile; (e) score_at_delivery vs outcome correlation (R²).

**49. Ошибка стратегии vs инфраструктуры**
🟡 Диагностика без кода по логам: (a) `activation_lag` = `activated_at − signal_published_at` > 5 мин → infrastructure (proxy/WS delay); (b) `stale_at < activation_at` → staleness filter не сработал или сработал поздно; (c) `barrier` triggered = инфраструктурный override, не стратегия; (d) random-looking SL без MFE на разных символах в одно время = stale WS snapshot.

**50. Один A/B тест на неделю по Telegram-сигналам**
🟡 **Тест: htf_conflict = True vs False → TP/SL ratio.** Сплит: взять историю 50–100 сигналов, разделить по полю `htf_conflict` в метаданных (если публикуется) или по времени (bounce-фаза vs trend-фаза). Сравнить % TP в каждой группе. Ожидание: htf_conflict=True должен давать ~2× больше SL. Если нет → фильтр не работает.

---

## Сводные рекомендации

| Проблема | Что менять | Приоритет |
|----------|-----------|-----------|
| SL на мемкоинах | min_stop_normalized → **max(1.5×ATR, текущее%)** | 🔴 P0 |
| MFE=0 + immediate SL | тег activation_lag в каждый outcome | P0 |
| 7/7 CVD-short в bounce | require htf_reversal_confirmation (1H BOS вверх = block) | P0 |
| pd_zone short в discount | hard-block (уже есть, убедиться что не bypassed) | P0 |
| ema_bounce + keltner = 1 leg, не 2 | пересчитать вес confluence | P1 |
| ADX hard-gate → soft weight | ADX<20 = −0.10 score, не reject | P1 |
| min_score 0.65 → 0.60 | высвободит валидные сетапы | P1 |
| N<100 на стратегию | не делать выводов до N≥100 | P1 |
| A/B по htf_conflict | запустить тест 50 следующих сигналов | P2 |
| barrier_short threshold | задать явно в config, не эмпирически | P2 |

**Честная граница:** Q12, Q13, Q17, Q19 (точные %) → только ваша телеметрия. Не приписывать конкретные цифры без данных.
