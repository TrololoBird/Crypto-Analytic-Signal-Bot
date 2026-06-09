# Ответы на ~200 вопросов (TF + каталог 42 стратегий)

**Источник ответов:** веб-ресёрч + математика/определения. Ваш проект НЕ анализировался.

## Легенда достоверности
- 🟢 **факт** — есть источник (API Binance, математика, формальное определение)
- 🟡 **консенсус практики** — общепринятое значение/подход, но без строгого независимого бэктеста
- 🔴 **нет публичных данных** — воспроизводимого бэктеста не существует; ответ даётся только вашей телеметрией/A-B

> Честное предупреждение: цифры win-rate / expired% / MFE по конкретным TF и стратегиям проприетарны. Любой источник, дающий их «уверенно», — маркетинг. Поэтому большинство вопросов «оптимальный TF для X» помечены 🔴, и единственный корректный ответ — A/B на ваших же данных.

## Опорные принципы (на них ссылаются ответы)
- **[√t]** 🟢 Волатильность/ATR масштабируются как √(отношение TF): ATR(1h)≈2·ATR(15m), ATR(4h)≈4·ATR(15m). Правило строго для iid+нормальности; в крипте (толстые хвосты, кластеризация) даёт смещение на хвостах. Следствия: пороги в ×ATR переносимы между TF **только если ATR пересчитан на том же TF**; **TTL и staleness держать в МИНУТАХ, не в барах** (иначе при 15m→1h окно молча растягивается ×4).
- **[HTF]** 🟢/🟡 Канон SMC: контекст/bias на старшем TF + триггер/вход на младшем; setup, согласованный с HTF, имеет выше hit-rate, чем контртрендовый. Источники: ICT/SMC материалы.
- **[fund]** 🟢 Funding на Binance больше НЕ фиксированные 8h — динамически 8h/4h/1h per-symbol в зависимости от величины ставки. Любой код с «8h» по умолчанию неверен.
- **[ob-stale]** 🟡 Публичные depth/bookTicker/forceOrder обновляются за доли секунды (ликвидации — 1 событие/символ/1000мс; OI REST ~6с). При ручном исполнении с задержкой 1–20 мин такой сигнал протухает до действия человека → не годится как самостоятельный сигнал, только как confluence-leg.
- **[disc]** 🟡 SMC/ICT — дискреционная популяризированная рамка; «evidence A» в вашем каталоге = субъективная уверенность, не статистический edge. Источники прямо пишут, что паттерны «не гарантируют исход».
- **[no-data]** 🔴 Нет публичного бэктеста → решается вашей телеметрией.

---

# ЧАСТЬ 1 — Документ 1: выбор таймфрейма (100 вопросов)

## Блок 1 — Выбор entry-TF (1–20)
1. 🔴 [no-data] Для ручного limit нет публичного «оптимального» TF. Логика: чем старше TF, тем шире зона/дольше живёт уровень → ниже expired, но меньше сигналов. Старт-гипотеза: 30m–1h для limit. Проверять A/B.
2. 🟡 «Мёртвая зона» 15m — расхожее мнение трейдеров (шумно для SMC-лимиток, медленно для momentum), но это нарратив, не доказанный факт. 15m часто туда попадает.
3. 🟡 [√t] Задержка 1–20 мин = доля бара: на 5m это 0.2–4 бара (фатально, chase), на 1h — <0.3 бара (терпимо). Чем больше задержка, тем выше нужный entry-TF.
4. 🟢 [√t] В ×ATR того же TF, не абсолютный %. Абсолютный % не масштабируется с режимом волатильности.
5. 🟢 [√t] В **минутах**. Бары неустойчивы при смене TF.
6. 🟡 [HTF] Зоны OB/FVG/BOS дольше живут без инвалидации на старшем TF (1h/4h). На 15m чаще «съедаются» шумом.
7. 🟢 [HTF] Да, стандарт: триггер на младшем + контекст на старшем. Типовые пары 5m/1h, 15m/4h, 1h/4h. Ratio см. Q19.
8. 🟡 [HTF] Публиковать на close TF, на котором подтверждён setup (обычно entry-TF), но bias — со старшего. «Oldest confirmed» снижает repaint.
9. 🔴 [no-data] Публичных сравнительных бэктестов 30m vs 1h с win-rate нет.
10. 🟡 5m как trigger-only (не entry) — разумно: ловит момент, но план/зона на 1h. Снижает chase, не уводя в скальпинг.
11. 🔴 [no-data] MFE/MAE>1 по TF — только ваша телеметрия.
12. 🟢 [fund][ob-stale] Funding/OI лаг в минутах делает sentiment-сетапы бессмысленными на 5–15m; адекватны на 1h+.
13. 🟢 [√t] ATR% одного символа на 1h ≈ 2× от 15m. TF под vol-фильтр выбирают так, чтобы порог режима совпадал с горизонтом удержания.
14. 🟡 Зрелые OSS-боты чаще делают **per-family TF map**, а не один глобальный entry-TF.
15. 🟡 Confirmation delay = +1 бар неустраним; его цена в минутах минимальна на старших TF. На 5m +1 бар критичен.
16. 🟡 Да: сессионные фильтры заметны на 15m, почти иррелевантны на 4h.
17. 🔴 [no-data] Публичного benchmark «healthy expired %» по TF нет.
18. 🟡 [√t] Шире бары → реже стоп-ханты (SL дальше от noise band), но и меньше сигналов.
19. 🟡 Расхожее HTF:entry = 4×–6× (15m/1h, 1h/4h). 12× встречается реже.
20. 🟡 Логичнее **вверх** (30m/1h plan + опционально 5m trigger), т.к. оба симптома (expired + immediate SL) указывают на конфликт ритма бара/задержки. Но доказательство — только A/B (Q1).

## Блок 2 — Limit/Market и TF (21–35)
21. 🟡 [HTF] Последняя структурная свеча значима на TF, где видна структура — обычно 1h для non-scalp.
22. 🟡 Reference = close trigger-TF.
23. 🟡 Market оправдан при импульсе/акцепте; не привязан жёстко к TF — зависит от того, есть ли retest. Нет retest → market.
24. 🟡 Slippage ≈ f(spread, размер, ликвидность); формулы «×TF» нет. Slippage растёт на младших/тонких.
25. 🟡 Да, на младшем TF больше ложных частичных активаций по фитилю.
26. 🟢 [√t] Staleness в ×ATR(entry_tf), в минутах.
27. 🟡 Да, decay rate пересчитывать при смене TF (в минутах он инвариантен — ещё аргумент за минуты).
28. 🔴 [no-data] Dual-plan — edge не доказан; риск confusion для ручного трейдера высок.
29. 🟡 [√t] Min hold 30 мин «гарантируется» статистически от ~15–30m бара; на 5m выходы по шуму вероятнее.
30. 🟡 [HTF] Разворотные сетапы чище на 1h, чем 15m (меньше ложных climax).
31. 🟡 [HTF] Prop-практика для тренд-фолло — 1h+ вход. 15m даёт больше whipsaw.
32. 🟢 [ob-stale] CVD/walls без tick-данных: CVD считается из aggTrades (есть на публичном WS), стенки — нет надёжно. Мин. TF для CVD не «бар», а окно агрегации (см. Q70).
33. 🟢 [√t] BOS-retest timeout масштабировать как **constant minutes**, не constant bars.
34. 🟡 Optimistic-wick bias в бэктесте limit-fill хуже всего на младших TF (фитиль «задевает» зону без реального fill).
35. 🔴 [no-data] Опросов «с какого TF чаще исполняют» нет.

## Блок 3 — SL/TP geometry vs TF (36–47)
36. 🟢 [√t] Swing-lookback привязывать к entry-TF; в минутах — инвариант.
37. 🟡 Единый RR-floor 1.9 для всех TF малоосмыслен — разные сетапы имеют разный естественный RR. Лучше per-family.
38. 🔴 [no-data] Достижимость TP3 (5R) по TF — только телеметрия. Логика: на 1h hold дольше → 5R реже, но чище.
39. 🟢 [√t] «SL inside noise band» измеряется как SL_distance / ATR(entry_tf): если <~1·ATR — внутри шума.
40. 🟢 ATR-период (14 Wilder) одинаковый по TF; масштабируется само значение ATR, не период.
41. 🟢 [√t] Time-stop — в минутах.
42. 🟡 BE-whipsaw чаще на младших TF.
43. 🟡 High-ATR%-SL — чаще проблема universe (мем-коины), чем TF; разделить через нормировку SL/ATR.
44. 🟡 MFE≈0 за <15мин ближе к «late entry/chase» (задержка) ИЛИ wrong direction; сам по себе TF не различает — нужен тег time_to_fill.
45. 🟡 Post-SL→TP — аргумент и за wider SL, и за больший entry-TF (оба расширяют noise band). Различать по тому, был ли SL < 1·ATR.
46. 🟡 R-multiple уже нормирован по риску; доп. нормировка по TF не нужна, если SL корректен.
47. 🟢 [√t] ATR-trail с entry-TF.

## Блок 4 — Confluence и regime vs TF (48–57)
48. 🟡 [HTF] HTF-leg(и) считать на старшем TF, entry-leg — на entry-TF. Смешивать close нельзя (repaint).
49. 🟡 RSI-пороги (70/30) формально TF-инвариантны, но на младших TF чаще пробиваются → можно ужесточать на LTF.
50. 🟡 ADX порог ~20–25 (Wilder) сопоставим между TF по смыслу, но абсолютное значение на 15m шумнее; «20 на 15m» ≉ «20 на 1h» по надёжности.
51. 🟡 [HTF] При entry=1h логичен HTF=4h. 4h «обязателен» — нет, но желателен.
52. 🟡 [ob-stale] Microstructure-leg (bookTicker) осмыслен только на младших TF и только как мгновенный фильтр; для 1h+ бесполезен.
53. 🟡 Веса confluence по TF — да, имеет смысл (вес HTF-leg выше).
54. 🟡 Reversal обычно требует больше подтверждений, чем trend; зависимость от TF — через шум (LTF → больше нужно).
55. 🟡 [HTF] BTC bias 4h + entry 15m — классический mismatch масштабов; лучше bias 4h + entry 1h.
56. 🟡 Source-of-truth режима — старший TF (4h/1d) для bias, entry-TF для тайминга.
57. 🔴 [no-data] Optimal score-floor (0.53 vs 0.70) по TF — только калибровка на телеметрии.

## Блок 5 — SMC/structure vs TF (58–69)
58. 🟢 [√t] Displacement в ×ATR того же TF, что и OB.
59. 🟢 [√t] OB max age приводить к **часам** для честного cross-TF сравнения.
60. 🟡 [HTF] Mitigation 50% более предиктивен на старшем TF.
61. 🟡 Swing length 3 vs 5 — функция шума, т.е. косвенно TF; на LTF берут больше (5+).
62. 🟢 [√t] FVG min gap в ×ATR(TF).
63. 🟡 [HTF] Sweep-фитиль — шум на 15m, сигнал на 1h+.
64. 🟡 [disc] Premium/discount для intraday non-scalp — диапазон 4h (иногда 1h). Daily слишком широк.
65. 🟡 [HTF] CHoCH/BOS-фильтр на HTF, вход на LTF; мин. LTF без repaint — обычно ≥15m (на 1–5m repaint-риск растёт).
66. 🟡 Включать неподтверждённый «хвост» (forming candle) = repaint-риск; для сигнального бота — не включать.
67. 🔴 [no-data] Structure-clarity score per-TF — калибровка.
68. 🔴 [no-data] Edge breaker-block по TF — нет публичных цифр.
69. 🟡 OSS SMC-боты чаще рекомендуют 1h–4h как primary; 15m считается шумным.

## Блок 6 — Orderflow/public data vs TF (70–77)
70. 🟢 CVD из aggTrade: min окно агрегации — не «бар», а 1–5 мин для non-scalp; tick-точность есть, но шумна <1м.
71. 🟡 [ob-stale] Walls живут секунды–минуты (часто спуфинг); persistence в барах мерить бессмысленно.
72. 🟢 [ob-stale] Depth REST snapshot ~6с; stale-порог для сигнала — секунды. Несовместимо с 15m баром как самостоятельный сигнал.
73. 🟢 [fund] OI/funding слишком медленны для 15m; адекватны 1h+. Плюс funding-кадэнс теперь переменный (8h/4h/1h).
74. 🟢 [ob-stale] Liquidation forceOrder — 1 событие/символ/1000мс; confluence-окно в минутах (3–15), не в барах.
75. 🟡 Spread/basis-стратегии TF-независимы по природе (микроструктурные), но требуют spot-стрима, которого у вас нет (см. #26 каталога).
76. 🔴 [no-data] False-positive climax по TF — телеметрия. Логика: 15m даёт больше ложных climax.
77. 🟡 [ob-stale] Orderflow-стратегии отключать ниже ранга ликвидности N независимо от TF; на тонких символах публичный orderflow = шум.

## Блок 7 — Architecture: TF as first-class (78–90)
78. 🟡 Да, `entry_tf` per strategy family — нормальный industry-паттерн.
79. 🟡 `context_tf` stack (LTF/MTF/HTF) в конфиге — да, встречается в OSS (напр. конфиги multi-TF фреймворков). Конкретной «канонической» схемы нет.
80. 🟡 [ob-stale] WS-бюджет: публичный combined-stream тянет десятки символов × несколько TF; узкое место — не klines, а depth/bookTicker (высокочастотные). Klines по 4–5 TF × 50–100 символов реалистично.
81. 🟢 Между closes (entry=1h) активировать только по aggTrade/markPrice (pending-limit активация), новый анализ — на close.
82. 🟡 Lazy per strategy лучше, чем готовить все frames для всех — экономит CPU/память.
83. 🟢 [√t] Hardcoded 15m в atr_pct/volume_ratio — anti-pattern (design drift). TF-agnostic: все производные считать из выбранного frame, пороги — в ×ATR(frame).
84. 🟢 [√t] Да: TTL-словари из баров → в минуты + per-family.
85. 🟢 Да, тег `entry_tf_used` на каждом outcome — обязателен для любого TF A/B.
86. 🟡 A/B на live public data без auto-trade: параллельная публикация сигналов на разных TF + сравнение expired%/adverse-SL%/MFE-MAE по тегам. Корректная методология — paper/forward-test, не backtest (избегает wick-bias).
87. 🟡 [√t] При 15m→1h первыми «ломаются»: TTL (×4 в барах), staleness, ширина SL/зоны (вдвое узки), счётчик сигналов (×0.25). Логика стратегий — нет.
88. 🔴 [no-data] Per-symbol TF (BTC 1h, мемы 15m) — может быть оптимально, но overkill без доказательства; начать с per-family.
89. 🟡 Снижение score-floor на 1h/4h — косвенный признак, что 15m-порог слишком строг, да.
90. 🟡 `configured_primary_timeframe` недостаточно; нужен strategy/family-level override.

## Блок 8 — Итоговые decision-вопросы (91–100)
91. 🟡 «Один TF на всё» для смешанного каталога — нет; разные семейства имеют разный естественный ритм.
92. 🟡 Если строго один TF для v2 non-scalp manual — **1h** компромисс (30m шумнее, 4h мало сигналов). Низкая уверенность.
93. 🟡 Hybrid (TF в метаданных сигнала, детектор выбирает) — реализуемо и разумно.
94. 🟡 Худший TF для limit-only — самый младший из используемых (5m): максимум ложных fill по фитилю + chase.
95. 🟡 Худший TF для market-heavy — тоже младший (chase/slippage), но по другой причине.
96. 🔴 [no-data] Монотонность expired×TF — гипотеза (убывает с ростом TF), не доказана.
97. 🔴 [no-data] Монотонность immediate-SL×TF — гипотеза, не доказана.
98. 🟡 Лит-ры «мин. длительность бара для ручного исполнения» нет строгой; практика: бар ≥ задержки×~5, т.е. при 5–20 мин задержки → бар ≥ 30m–1h.
99. 🟡 Сдвиг сообщества от 15m intraday в 2024–2026 — есть нарратив в сторону HTF/SMC, но это не измеренный тренд.
100. 🔴/🟡 Финальная таблица family→entry_tf/context_tf/order/TTL — см. ЧАСТЬ 3 (заполнено как best-practice старт-гипотезы, не как доказанные значения).

---

# ЧАСТЬ 2 — Документ 2: каталог 42 стратегии (84 вопроса × 2 + 8 мета)

> Формат: setup_id — Q1 / Q2. Вердикт = KEEP / RETUNE / DISABLE + рекоменд. entry-TF и order (старт-гипотеза).

### Structure / SMC (1–10)
**structure_pullback** (limit) — Q1: 🟡 [HTF][√t] retest на 1h, триггер 15m; TTL в минутах (~ширина бара×N). Q2: 🟡 pullback-long в BTC-downtrend 4h — лучше score-penalty + повышенный порог, чем hard block (бенчмарк SL — 🔴 телеметрия). **Вердикт: KEEP, RETUNE TF→1h/limit.**

**structure_break_retest** (limit; 1h/15m) — Q1: 🟡 [HTF] ждать touch на 1h-уровне с 15m-подтверждением. Q2: 🟢 [√t] timeout в **часах**, не барах. **KEEP, это эталонный паттерн.**

**wick_trap_reversal** (limit) — Q1: 🟡 activation по **close back inside** (фитиль = больше ложных). Q2: 🟡 min wick ~1.5–2×ATR / >50% range; на альтах порог выше (шум). **KEEP, RETUNE на close-confirm.**

**fvg_setup** (limit) — Q1: 🟡 [HTF] 1h FVG + 15m вход даёт выше fill-rate, чем 15m FVG. Q2: 🟡 partial 50% — treat as mitigation, не invalidate. min gap 🟢[√t] в ×ATR(TF). **KEEP, RETUNE TF.**

**order_block** (limit) — Q1: 🟢[disc] OB = последняя противоположная свеча перед displacement + обязательный FVG + слом структуры; «displacement» формально = body% / range>1.5–2×ATR (точных OSS-констант нет, 🟡). Q2: 🟢[√t] max age сравнивать в часах (24×1h ≈ 96×15m); 72×15m=18ч < 24×1h=24ч. **KEEP.**

**liquidity_sweep** (limit) — Q1: 🟡 limit-вход в пределах ~1–3 баров после sweep, иначе setup dead. Q2: 🟡 equal H/L: min 2 касания (3 надёжнее). **KEEP.**

**bos_choch** (limit) — Q1: 🟡 [HTF] CHoCH/BOS как событие — на HTF, вход — на LTF. Q2: 🟡 вход на origin-OB CHoCH надёжнее, чем на break-level. **KEEP.**

**breaker_block** (limit) — Q1: 🔴 win-rate delta vs OB на крипте — нет публичных цифр. Q2: 🟢[disc] инвалидация по close телом через 50% — стандартное правило. **KEEP (но edge не доказан, [disc]).**

**turtle_soup** (limit; 1h/15m) — Q1: 🟡 lookback 20 баров (классика Turtle); на 1h окно надёжнее. Q2: 🟡 limit у failed-breakout level. **KEEP, RETUNE→1h.**

**fakeout_detector** (limit, ev.B) — Q1: 🟡 алгоритмически fakeout ≈ liquidity_sweep без reclaim-структуры; различимы слабо → высокий риск дубля. Q2: 🔴 min OOS edge — телеметрия. **MERGE-кандидат с liquidity_sweep / RETUNE.**

### Trend continuation
**ema_bounce** (market) — Q1: 🟡 «market» для pullback — аномалия; логичнее limit у EMA-зоны. Q2: 🟡 EMA20 для 1h non-scalp. **RETUNE: order→limit.**

**vwap_trend** (limit) — Q1: 🟡 на 24/7 крипте rolling/anchored VWAP лучше session-VWAP. Q2: 🟢[√t] зона ±0.25×ATR предпочтительнее абсолютного %. **KEEP, RETUNE→rolling VWAP.**

**supertrend_follow** (market) — Q1: 🟡 Supertrend(ATR10, mult3) flip как market — поздно для ручного входа с задержкой; рассмотреть limit на pullback к ST-линии. Q2: 🟡 фильтр на стеке 1h+4h. **RETUNE.**

**multi_tf_trend** (market; 1h/4h) — Q1: 🟡 публиковать на close 1h (не 15m). Q2: 🟡 да — то, что единственная «чистая» HTF-стратегия, косвенно поддерживает гипотезу о 15m-перекосе (но не доказывает). **KEEP, это шаблон.**

### Breakout / momentum
**squeeze_setup** (market) — Q1: 🟡 limit на retest к пробитой полосе чище, чем market на breakout-баре (меньше chase). Q2: 🟡 min длительность сжатия ~6–10 баров. **RETUNE.**

**keltner_breakout** (market) — Q1: 🟡 close вне канала (EMA20±2×ATR10), не walk; non-repaint. Q2: 🟡 фильтр ADX>20–25 + объём. **KEEP, RETUNE на close-confirm.**

**session_killzone** (limit, breakout) — Q1: 🟡 killzones в крипте — перенос из форекса; объём в US/EU-часы реален, но торговый edge не доказан, риск confirmation bias. Q2: 🟡 limit у края pre-session range. **RETUNE → весовой фильтр, не самостоятельная стратегия.**

**price_velocity** (market) — Q1: 🟡 z-score доходностей надёжнее %/мин (нормирован на волатильность). Q2: 🔴 median time-to-TP1 — телеметрия. **RETUNE на z-score.**

**volume_anomaly** (market, vol≥1.6×) — Q1: 🔴 adverse excursion первые 5 мин — телеметрия (вероятно высок: market-chase). Q2: 🟢 вход на next-bar-open vs close: close-входы в бэктесте дают lookahead-bias. **RETUNE: вход next-open.**

**volume_climax_reversal** (market) — Q1: 🟡 climax = vol>N×MA(объём) & range>M×ATR (N~2–3, M~1.5–2; точных крипто-констант нет). Q2: 🟡 short climax против Supertrend↑ — требовать HTF-exhaustion, иначе disable. **RETUNE.**

**bb_squeeze** (market) — Q1: 🟡 TTM-squeeze: BB(20,2) внутри Keltner = сжатие; fire на расширении. Q2: 🟡 bandwidth в нижних ~20% за 100 баров. **MERGE-кандидат со squeeze_setup.**

**atr_expansion** (market) — Q1: 🟡 во многом дублирует squeeze/velocity; уникальный edge не очевиден. Q2: 🟡 expansion-after-expansion (ATR уже высок) — skip (mean-reversion риск). **MERGE/DISABLE-кандидат.**

### Divergence / reversal
**funding_reversal** (market; 1h/15m) — Q1: 🟢[fund] порог зависит от монеты; «экстремум» относительный (z-score funding), не абсолютные 0.03 vs 0.1. Q2: 🟢[fund] публиковать по фактическому funding-снапшоту (кадэнс 8h/4h/1h per-symbol!), вход к 1h. **RETUNE: учесть переменный кадэнс.**

**hidden_divergence** (market) — Q1: 🟡 для non-scalp лучше limit на pullback, чем market. Q2: 🟡 RSI-дивер устойчивее MACD на шумном 15m. **RETUNE order→limit.**

**indicator_divergence** (market) — Q1: 🟡 требовать confirm-bar (снижает MFE≈0/мгновенный SL). Q2: 🟡 min 2 пивота (3 надёжнее). **RETUNE: confirm-bar.**

**cvd_divergence** (market) — Q1: 🟢 CVD из публичного aggTrade (поле maker/taker); min магнитуда — относительная, калибровать. Q2: 🟡 CVD-short в аптренде — высокий fail; только с HTF-exhaustion. **KEEP, RETUNE.**

**cvd_exhaustion** (market, ev.B) — Q1: 🟡 exhaustion = спад наклона дельты vs divergence = расхождение с ценой; различие тонкое. Q2: 🟡 сильно пересекается с cvd_divergence. **MERGE-кандидат.**

**rsi_divergence_bottom** (market) — Q1: 🟡 limit у div-low надёжнее market на signal-bar. Q2: 🟡 [HTF] 1h div + 15m триггер. **RETUNE.** (Также вероятный дубль с indicator_divergence/hidden_divergence — проверить overlap.)

**orderflow_imbalance** (limit, ev.B) — Q1: 🟢[ob-stale] bookTicker-имбаланс persist секунды; min persist до публикации — секунды, что несовместимо с ручным limit. Q2: 🟡 order type спорен. **DISABLE / confluence-leg.**

**pinbar_reversal** (limit, ev.B) — Q1: 🟡 wick≥2×body, тело в верхней/нижней трети. Q2: 🟡 только у HTF S/R — обязательный фильтр. **KEEP, RETUNE: + HTF-фильтр.**

### Orderbook / orderflow (evidence C на 25–27)
**whale_walls** (limit, C; SL-контрибьютор) — Q1: 🟡[ob-stale] публичные стенки часто спуфинг; фильтр по age/size ненадёжен без L2-истории. Q2: 🟢 ev.C + вклад в SL → **DISABLE для signal-only** (либо доказать edge форвард-тестом).

**spread_strategy** (limit, C) — Q1: 🟡 perp spread/basis без spot-стрима — архитектурный тупик для вашей конфигурации. Q2: 🟡 TTL — секунды. **DISABLE (нет spot-данных).**

**depth_imbalance** (limit, C) — Q1: 🟢[ob-stale] TOB обновляется 100мс–1с vs 15m бар = фундаментальный mismatch. Q2: 🟡 ratio+duration в секундах. **DISABLE / confluence-leg.**

**absorption** (limit, A) — Q1: 🟡 частично детектируется из kline+volume (без полной L2-истории — приближённо). Q2: 🟡 close-confirm надёжнее фитиля. **KEEP, RETUNE.**

**aggression_shift** (market, B) — Q1: 🟡 min 2–3 последовательных бара дельты до публикации. Q2: 🟡 лучше как confluence-leg, чем standalone. **RETUNE → leg.**

### Liquidity
**liquidation_heatmap** (market, B) — Q1: 🟢[ob-stale] forceOrder = 1/символ/1000мс (агрегат неполный); кластеры строить по накоплению за минуты; дистанция ~0.5–2% от цены. Q2: 🟡 limit *перед* кластером лучше market *в* кластер. **KEEP, RETUNE.**

**stop_hunt_detection** (market, A) — Q1: 🟡 во многом = liquidity_sweep (фитиль за swing + reclaim). Q2: 🟡 market на reclaim-close. **MERGE-кандидат с liquidity_sweep.**

### Sentiment / multi-asset
**ls_ratio_extreme** (market; trigger 4h) — Q1: 🟢 4h trigger + 15m entry = mismatch; вход к 1h/4h. Q2: 🟡 globalLongShortAccountRatio vs topTrader — top-trader обычно информативнее (🔴 строгого сравнения нет). **RETUNE: выровнять entry-TF.**

**oi_divergence** (market; trigger 4h) — Q1: 🟢[fund] OI REST ~6с, openInterestHist от 5m; «4h-лаг» — это ваш период выборки, не лаг данных; max stale — минуты. Q2: 🔴 win-rate по режиму — телеметрия. **KEEP, RETUNE entry-TF.**

**btc_correlation** (market, B) — Q1: 🔴 lead/lag альтов к BTC в минутах — телеметрия (обычно секунды–минуты). Q2: 🟡 rolling β окно ~50–100 баров. **RETUNE.**

**altcoin_season_index** (market; 1h/1h) — Q1: 🟡 из public-only считается приближённо (доля альтов, обгоняющих BTC за N дней) — шумно. Q2: 🟡 да, единственная чистая 1h — шаблон миграции остальных. **KEEP как шаблон.**

### Wyckoff / special
**wyckoff_spring** (limit; 1h) — Q1: 🟡 публиковать на close 1h. Q2: 🟡 spring vs sweep различаются контекстом (spring — в trading range Вайкоффа); алгоритмически тонко. **KEEP.**

## Мета-вопросы каталога (1–8)
1. 🟡 23 market vs 19 limit — крен в market = chase-bias для ручного non-scalp; желательно сместить к limit.
2. 🟡 Evidence C в проде — индустрия чаще карантинит/держит в shadow-mode до доказательства. Рекомендую quarantine.
3. 🟡 4 «новых» (fakeout_detector, cvd_exhaustion, orderflow_imbalance, pinbar_reversal) — вероятные дубли существующих семейств; кандидаты на merge.
4. 🟡 15 reversal-стратегий — риск кучи коррелированных шортов в bear-фазе; нужен дедуп по корреляции срабатываний.
5. 🔴/🟡 Per-strategy таблица — см. ЧАСТЬ 3 (старт-гипотезы).
6. 🟡 Да — pattern_tf ≠ trigger_tf (HTF+LTF) логично сделать **дефолтом** редизайна каталога.
7. 🟡 Единый min_rr=1.9 для 42 — нет; per-family RR-floor (reversal часто 1.5–2R, trend-runner 3R+).
8. 🟡 Сколько из 42 оставить на 15m-trigger — доказательно: НИ ОДНУ «по умолчанию»; решает A/B. Гипотеза: SMC/structure → 1h, sentiment → 4h, momentum → 15m trigger допустим.

---

# ЧАСТЬ 3 — Итоговые decision-таблицы

> ⚠️ Все значения ниже — **старт-гипотезы (🟡), а не доказанные оптимумы (🔴)**. entry_tf/order — best-practice по принципам [√t]/[HTF]/[fund]/[ob-stale]; TTL_min — порядок величины (≈ длительность entry-бара × 2–4). Финальные значения даёт только ваш forward-A/B.

| # | setup_id | entry_tf | context_tf | order | TTL_min | verdict |
|---|----------|----------|------------|-------|---------|---------|
|1|structure_pullback|1h|4h|limit|~120|RETUNE|
|2|structure_break_retest|1h|4h|limit|~120|KEEP|
|3|wick_trap_reversal|15m|1h|limit|~45|RETUNE(close-confirm)|
|4|squeeze_setup|15m|1h|limit(retest)|~45|RETUNE|
|5|ema_bounce|1h|4h|limit|~120|RETUNE(order)|
|6|fakeout_detector|15m|1h|limit|~45|MERGE/RETUNE|
|7|fvg_setup|1h|4h|limit|~120|RETUNE(TF)|
|8|order_block|1h|4h|limit|~180|KEEP|
|9|liquidity_sweep|15m|1h|limit|~45|KEEP|
|10|bos_choch|1h|4h|limit|~120|KEEP|
|11|funding_reversal|1h|4h(funding)|market|~120|RETUNE(кадэнс)|
|12|hidden_divergence|1h|4h|limit|~120|RETUNE(order)|
|13|indicator_divergence|1h|4h|market+confirm|~90|RETUNE|
|14|keltner_breakout|15m|1h|market(close)|~45|RETUNE|
|15|cvd_divergence|15m|1h|market|~45|KEEP|
|16|cvd_exhaustion|15m|1h|market|~45|MERGE→15|
|17|session_killzone|—|—|filter|—|RETUNE→leg|
|18|breaker_block|1h|4h|limit|~120|KEEP([disc])|
|19|turtle_soup|1h|4h|limit|~120|RETUNE(TF)|
|20|vwap_trend|1h|4h|limit|~90|RETUNE(rolling)|
|21|supertrend_follow|1h|4h|limit(pullback)|~120|RETUNE|
|22|price_velocity|5m→15m|1h|market(z)|~30|RETUNE(z-score)|
|23|volume_anomaly|15m|1h|market(next-open)|~30|RETUNE|
|24|volume_climax_reversal|1h|4h|market|~90|RETUNE|
|25|whale_walls|—|—|—|—|**DISABLE**|
|26|spread_strategy|—|—|—|—|**DISABLE**(нет spot)|
|27|depth_imbalance|—|—|leg|—|**DISABLE/leg**|
|28|absorption|15m|1h|limit|~45|RETUNE|
|29|aggression_shift|—|—|leg|—|RETUNE→leg|
|30|liquidation_heatmap|15m|1h|limit(ahead)|~45|RETUNE|
|31|stop_hunt_detection|15m|1h|market|~30|MERGE→9|
|32|multi_tf_trend|1h|4h|market|~120|KEEP(шаблон)|
|33|rsi_divergence_bottom|1h|4h|limit|~120|RETUNE|
|34|wyckoff_spring|1h|4h|limit|~180|KEEP|
|35|bb_squeeze|15m|1h|market|~45|MERGE→4|
|36|atr_expansion|15m|1h|market|~45|MERGE/DISABLE|
|37|ls_ratio_extreme|1h–4h|1d|market|~240|RETUNE|
|38|oi_divergence|1h|4h|market|~120|RETUNE|
|39|btc_correlation|15m|1h|market|~30|RETUNE|
|40|altcoin_season_index|1h|1d|market|~240|KEEP(шаблон)|
|41|orderflow_imbalance|—|—|leg|—|**DISABLE/leg**|
|42|pinbar_reversal|1h|4h|limit|~120|RETUNE(+HTF)|

## Top-10 по ожидаемому эффекту от исправления TF/геометрии
(там, где симптомы expired/MFE≈0/SL-noise максимально вероятны; 🔴 ранжирование — гипотеза)
1. fvg_setup 2. order_block 3. structure_pullback 4. structure_break_retest 5. bos_choch 6. liquidity_sweep 7. turtle_soup 8. vwap_trend 9. wick_trap_reversal 10. keltner_breakout
— все SMC/structure-limit семейства, где [√t]+[HTF] бьют по узкой зоне/короткому TTL сильнее всего.

## Top-10 кандидатов на DISABLE (public-only)
1. whale_walls (C, спуфинг, SL-вклад) 2. spread_strategy (нет spot) 3. depth_imbalance (C, sub-sec mismatch) 4. orderflow_imbalance (sub-sec) 5. atr_expansion (дубль) 6. aggression_shift→leg 7. session_killzone→filter 8. cvd_exhaustion (дубль→merge) 9. bb_squeeze (дубль→merge) 10. fakeout_detector (дубль→merge)
— причины: [ob-stale] (1–4,6), дубль/слабый уникальный edge (5,8,9,10), не стратегия а фильтр (7).

## Пары на слияние (проверить по корреляции срабатываний на телеметрии)
- liquidity_sweep ⇄ stop_hunt_detection ⇄ fakeout_detector (механика sweep+reclaim)
- cvd_divergence ⇄ cvd_exhaustion (дельта-расхождение)
- squeeze_setup ⇄ bb_squeeze ⇄ atr_expansion (волатильность-сжатие/расширение)
- indicator_divergence ⇄ hidden_divergence ⇄ rsi_divergence_bottom (дивергенции)
- order_block ⇄ breaker_block (failed OB = breaker)

---

# Что осталось принципиально без ответа (честно)
Эти классы вопросов **не закрываются вебом ни при каком поиске** — нет воспроизводимых публичных данных:
- абсолютные win-rate / expired% / MFE-MAE по каждому TF и стратегии;
- benchmark «healthy» метрик по TF;
- монотонность expired×TF и SL×TF;
- ранжирование «худший/лучший TF» в цифрах.

Единственный валидный источник для них — **ваш forward-A/B с тегом `entry_tf_used`** и переводом всех порогов в `k·ATR(entry_tf)` + TTL в минутах. Это снимает смешение переменных (TF / ширина зоны / ширина SL / задержка исполнения), которое сейчас делает невозможным приписать 75% expired именно таймфрейму.
