# Scanner (Охотник) — Полная методика обнаружения манипуляций

## 0. Архитектура (REST + WS)

Система состоит из трёх циклических процессов:

```
ЦИКЛ A: СКРИНИНГ РЫНКА (каждые 15-30 мин)
  REST /ticker/24hr → 2000+ USDT пар (1 запрос)
  → Фильтр: объём ≥ 10M USD, change 3-80%, возраст > 7 дней
  → Топ-50 кандидатов по скору расширенной готовности

ЦИКЛ B: ЗАГРУЗКА КОНТЕКСТА + ДЕТЕКЦИЯ (каждые 5-15 мин)
  Для каждого кандидата:
    REST fetch 1d(180) + 4h(120) + 1h(120) + 15m(200)
  → Polars batch: ATR, rolling body, swing pivots, BOS/CHoCH, bokovik
  → State machine: проверка цепочек событий
  → Если score ≥ 0.50 → сигнал + WS подписка 1m

ЦИКЛ C: WS ENTRY (реальное время, после сигнала)
  subscribe 1m на символы с score ≥ 0.50
  → Агрегация 1m → 5m → 15m
  → Поиск BOS/CHoCH на 5m для входа
  → Если подтверждение → deliver точную цену входа
```

---

## 1. Иерархия таймфреймов

Анализ идёт строго сверху вниз. Каждый ТФ имеет свою роль.

### 1.1 Роли ТФ

| ТФ | Роль | Источник | Глубина | Что даёт |
|----|------|----------|---------|----------|
| **1d (daily)** | Макро-контекст | REST fetch | 180 свечей | Макро-экстремум (цель для свипа), направление глобального тренда |
| **4h** | Мезо-основной | REST fetch | 120 свечей | Боковики, свипы, импульсы, поглощения — **основной ТФ детекции** |
| **1h** | Мезо-альтернативный | REST fetch | 120 свечей | Альтернативный поиск боковиков/свипов, если 4h не сформирован |
| **15m** | Микро-контекст | REST fetch | 200 свечей | Базовая волатильность, предварительный слом структуры |
| **5m** | Микро-вход | WS агрегация | 96 свечей (8ч) | BOS/CHoCH — финальное подтверждение входа |
| **1m** | WS-стрим | WS subscribe | агрегация → 5m | Сырой поток, из которого собираем 5m+ |

### 1.2 Зависимость ТФ в REST цикле

| Зависимость | Причина |
|-------------|---------|
| 1d → 4h | Макро-экстремум нужен перед проверкой мезо-свипа |
| 4h → 1h | Pattern A: на 4h импульс+поглощение, на 1h альтернативный боковик |
| 4h → 15m | Pattern B: после 4h/1h затухания сверяем LTF на 15m |
| 15m → 5m | Только 5m WS — не загружаем REST, агрегируем из 1m WS |

### 1.3 Какие ТФ НЕ используются сканером

| ТФ | Почему нет |
|----|------------|
| 1w | Нет в транскрипциях. 1d даёт достаточно контекста. Только Pattern B проверяет тренд — через 1d HH/HL |
| 12h, 8h | Нет в транскрипциях. 4h+1h достаточно для мезо |
| 30m | Нет необходимости — 15m → 5m скачок достаточен |

---

## 2. Три уровня структуры

### 2.1 Макро (1d, 180 свечей)

**Роль**: определить ключевые экстремумы, которые будут целями свипа.

```
macro_high = MAX(high[0:172])   // 180 - 7 последних - 1
macro_low  = MIN(low[0:172])    // 180 - 7 последних - 1
```

Последние 7 свечей исключены — они могут быть частью самого свипа (чтобы не детектить свип текущей свечи самой себя).

**Роль в Pattern A**: macro_low — если цена ниже него, значит даунтренд глубокий, лонг не берём.
**Роль в Pattern B**: macro_high — цель для финального свипа.

### 2.2 Мезо (4h, 1h, 120 свечей)

**Роль**: основное поле детекции. Все события происходят здесь.

```
pattern_a_context = fetch 4h(120) + 1h(120)
pattern_b_context = fetch 4h(120)
```

**Проверяемые события на мезо:**
1. Свип макро-уровня (Pattern B)
2. Импульс + поглощение (Pattern A)
3. Боковик и его касания (Pattern A)
4. Затухание свечей (Pattern B)

### 2.3 Микро (15m REST, 5m — из WS 1m)

**Роль**: подтверждение разворота.

```
15m(200) — загружается REST раз в 5-15 минут (контекст)
5m       — собирается из WS 1m подписки после сигнала (entry timing)
```

---

## 3. Два паттерна манипуляций

### ПАТТЕРН A: Поглощение → Боковик → Свип → Памп (LONG)

**Транскрипция**: Yesports +160%, BSB +250%, Hey +100%.

#### 3.1 Полная последовательность

```
ШАГ 1: Агрессивный памп (импульс)       — 4h
  Сильное движение вверх на 5-20%+.
  Тело одной свечи ≥ 1.5× ATR(14).
  ИЛИ ≥3 последовательных зелёных свечи с общим движением ≥ 1.0× ATR.

ШАГ 2: Поглощение одной свечой           — тот же ТФ (4h)
  Одна свеча перекрывает ≥ 60% диапазона предыдущего пампа.
  Закрывается обратно, цена возвращается к уровню ДО пампа.
  *Это ключевой сигнал*: крупный игрок собрал лонгов.

ШАГ 3: Боковик-1 (первый)                — 4h/1h
  После поглощения формируется диапазон.
  Ширина 1-15% цены. ≥3 касания границ.
  ATR сжимается до ≤ 70% от ATR до поглощения.

ШАГ 4: Свип вниз                         — тот же ТФ (4h/1h)
  Цена пробивает нижнюю границу боковика.
  Хвост свечи ≥ 30% от всего диапазона свечи.
  Закрытие ВНУТРИ боковика.
  *Сбор стопов лонгистов, зашедших в боковике.*

ШАГ 5: Боковик-2 (второй) ← Я пропустил это! — 4h/1h
  После свипа цена возвращается и формирует НОВЫЙ боковик.
  Отличается от боковика-1: более узкий, объём растёт.
  *Из этого боковика будет настоящий памп.*

ВАРИАНТ (если свипа не было):
  Боковик-1 продолжается → нужен закреп выше хая боковика
  + бычьи объёмы → вход на пробое

ШАГ 6: Слом структуры вверх             — 15m (REST) → 5m (WS)
  BOS вверх: close > prev_swing_high × 1.003
  CHoCH бычий: после LH/LL close > last_LH × 1.003
  *Обязательное условие: объём ≥ 1.5× среднего*

ШАГ 7: ВХОД LONG                        — 5m (WS)
  После LTF-подтверждения.

ШАГ 8: ЦЕЛИ
  TP1: первый свинг-хай выше входа
  TP2: хай предыдущего пампа
  TP3: следующая зона накопления
```

#### 3.2 Подвариант A2: Из восходящего канала → вниз → боковик

Из транскрипции: «цена сформировала максимум, показала манипуляцию вниз и начала формировать нисходящий канал и позже ушла в боковик с накоплением ликвидности снизу и сверху. Обычно после таких движений есть **две вариации**: или выход вниз, или **чаще** — закреп выше хая».

```
ШАГ 1: Восходящий канал                 — 1d/4h
ШАГ 2: Манипуляция вниз (свип)          — 4h
ШАГ 3: Нисходящий канал                 — 4h/1h
ШАГ 4: Боковик (накопление)             — 4h/1h
ШАГ 5: Два варианта:
        A) Пробой вниз — полное поглощение пампа
        B) Закреп выше хая канала → LONG (чаще)
ШАГ 6: Условие для B: закреп + бычьи объёмы ≥ 1.5×
```

#### 3.3 Подвариант A3: Без начального пампа (чистое накопление)

Из транскрипции BSB: «долго-долго идёт в нисходящем канале, постепенно обновляет минимумы, без пампа».

```
ШАГ 1: Нисходящий канал — 4h/1h (без пампа)
ШАГ 2: Боковик (накопление) — 4h/1h
ШАГ 3: Накопление ликвидности (много касаний, объём растёт)
ШАГ 4: Слом структуры вверх — 15m/5m
ШАГ 5: Вход LONG

Характеристики:
  - Касаний ≥ 5 (накопление дольше, раз нет импульса)
  - Объём растёт к нижней границе
  - Score ниже, чем с импульсом (памп ДО = сильнее движение)
```

---

### ПАТТЕРН B: Тренд → Финальный свип → Затухание → Разворот (SHORT)

**Транскрипция**: GTC — вход с пика, -30% до первого TP.

#### 3.4 Полная последовательность

```
ШАГ 1: HTF-тренд восходящий              — 1d
  HH/HL серия. ADX условно > 25.

ШАГ 2: Серия импульс → поглощение        — 4h/1h
  Каждый импульс вверх поглощается обратно.
  Это норма внутри тренда, не сигнал.

ШАГ 3: Финальный импульс (свип хая)      — 4h → сверка с 1d macro_high
  Цена обновляет macro_high (максимум за 180 дней).
  Свеча ≥ 0.5% выше macro_high.
  *Это последний хай перед разворотом.*

ШАГ 4: **Немедленный набор части позиции** ← Я пропустил!
  Из транскрипции: «когда обновляют предыдущий максимум —
  сразу же можно набирать частично позиции в шорт, если
  выше уже ликвидности нету».
  Условие: swing_highs выше current_price × 1.005 ≤ 1.

ШАГ 5: Локальный боковик у вершины       — 4h/1h ← Я пропустил!
  После свипа формируется маленький диапазон прямо у хая.
  *Это пауза перед разворотом, а не продолжение тренда.*

ШАГ 6: Затухание свечей (candle fade)   — 4h/1h
  avg_body(last 8) / avg_body(prior 8) ≤ 0.5
  avg_range(last 8) / avg_range(prior 8) ≤ 0.6

ШАГ 7: Красная импульсная свеча          — 4h/1h
  Пробой локального боковика вниз.
  Тело свечи ≥ 1.5× ATR.

ШАГ 8: LTF-подтверждение                 — 15m (REST) → 5m (WS)
  BOS вниз: close < prev_swing_low × 0.997
  CHoCH медвежий.
  *Обязательно: объём ≥ 1.5× среднего* ← я это тоже упускал

ШАГ 9: ДОБОР позиции                     — 5m (WS)
  После LTF-подтверждения добираем остаток объёма.

ШАГ 10: ЦЕЛИ
  TP1: первый свинг-лой ниже входа
  TP2: нижняя граница боковика/зона накопления
  TP3: предыдущий значимый лой
```

---

## 4. Технические индикаторы и формулы

### 4.1 ATR (Average True Range)

```python
atr(bars, period=14):
    total = 0
    for i in range(len-14, len):
        h,l,pc = bars[i].high, bars[i].low, bars[i-1].close
        tr = max(h-l, abs(h-pc), abs(l-pc))
        total += tr
    return total / 14
```

Где используется:
- Pattern A шаг 1: импульс = body ≥ 1.5 × atr(14)
- Pattern B шаг 6: затухание — не ATR, а body/range ratio
- Pattern A шаг 3: сжатие ATR = atr_ratio = current_atr / prior_atr ≤ 0.7

### 4.2 Swing pivots (фрактал n=3)

```python
swing_high(bars, i):
    return (bars[i].high > bars[i-1].high and
            bars[i].high > bars[i-2].high and
            bars[i].high >= bars[i+1].high and
            bars[i].high >= bars[i+2].high)

swing_low(bars, i):
    return (bars[i].low < bars[i-1].low and
            bars[i].low < bars[i-2].low and
            bars[i].low <= bars[i+1].low and
            bars[i].low <= bars[i+2].low)
```

Где используется:
- BOS/CHoCH (шаг 6 Pattern A, шаг 8 Pattern B)
- Боковик: границы — это не просто min/max, а свинг-точки
- Нет ликвидности: подсчёт свинг-хаёв выше цены

### 4.3 BOS (Break of Structure)

```python
bos_up(bars, buffer=0.003):
    hh_last = max(high[-20:])
    return close[-2] <= hh_last and close[-1] > hh_last * 1.003

bos_down(bars, buffer=0.003):
    ll_last = min(low[-20:])
    return close[-2] >= ll_last and close[-1] < ll_last * 0.997
```

### 4.4 CHoCH (Change of Character)

```python
choch_bull(bars, buffer=0.003):
    swing_highs = [...]  # все свинг-хаи
    lh = последний swing_high (ниже предыдущего — lower high)
    return close[-2] <= lh and close[-1] > lh * 1.003

choch_bear(bars, buffer=0.003):
    swing_lows = [...]
    hl = последний swing_low (выше предыдущего — higher low)
    return close[-2] >= hl and close[-1] < hl * 0.997
```

### 4.5 Импульс

```python
is_impulse(bar, atr14):
    body = abs(bar.close - bar.open) / bar.open
    return body >= 1.5 * atr14

is_consecutive_impulse(bars, atr14, min_count=3):
    count = 0
    direction = None
    for bar in reversed(bars):
        dir = "up" if bar.close > bar.open else "down"
        if direction is None:
            direction = dir
        elif dir != direction:
            break
        count += 1
    return count >= min_count
```

### 4.6 Поглощение

```python
is_absorbed(impulse_end_price, current_price, impulse_start_price):
    retrace = abs(current_price - impulse_end_price)
    impulse_range = abs(impulse_end_price - impulse_start_price)
    return retrace / impulse_range >= 0.80

is_one_candle_absorption(candle_body, impulse_range):
    return candle_body / impulse_range >= 0.60
```

### 4.7 Боковик

```python
is_bokovik(bars, window=30):
    lo = min(bars[-window:], key=lambda b: b.low)
    hi = max(bars[-window:], key=lambda b: b.high)
    width_pct = (hi - lo) / ((lo + hi) / 2) * 100
    
    if width_pct < 1 or width_pct > 15: return False
    
    touch_buffer = width_pct * 0.05 / 100 * mid
    touches_lo = count(|b.low - lo| <= touch_buffer)
    touches_hi = count(|b.high - hi| <= touch_buffer)
    
    touches = touches_lo + touches_hi
    if touches < 3: return False
    
    current_atr = atr(bars[-window:])
    prior_atr = atr(bars[-window*2:-window])
    atr_ratio = current_atr / prior_atr
    
    return atr_ratio <= 0.70
```

### 4.8 Свип

```python
is_sweep_high(bars, level):
    for bar in reversed(bars):
        if bar.high > level:
            total_range = bar.high - bar.low
            upper_wick = bar.high - max(bar.close, bar.open)
            wick_ratio = upper_wick / total_range
            return wick_ratio >= 0.30 and bar.close <= level
    return False

is_sweep_low(bars, level):
    for bar in reversed(bars):
        if bar.low < level:
            total_range = bar.high - bar.low
            lower_wick = min(bar.close, bar.open) - bar.low
            wick_ratio = lower_wick / total_range
            return wick_ratio >= 0.30 and bar.close >= level
    return False
```

### 4.9 Затухание свечей (Candle Fade)

```python
def candle_fade(bars, n=8):
    recent = bars[-n:]
    prior = bars[-n*2:-n]
    
    avg_body_rec = mean(abs(b.close - b.open) for b in recent)
    avg_body_pri = mean(abs(b.close - b.open) for b in prior)
    body_ratio = avg_body_rec / avg_body_pri
    
    avg_range_rec = mean(b.high - b.low for b in recent)
    avg_range_pri = mean(b.high - b.low for b in prior)
    range_ratio = avg_range_rec / avg_range_pri
    
    return body_ratio <= 0.50 and range_ratio <= 0.60
```

### 4.10 Нет ликвидности

```python
def no_liquidity_above(bars, current_price):
    swing_highs = all_swing_highs(bars)  # из всех ТФ
    above = [h for h in swing_highs if h > current_price * 1.005]
    return len(above) <= 1  # только что обновлённый хай

def no_liquidity_below(bars, current_price):
    swing_lows = all_swing_lows(bars)
    below = [l for l in swing_lows if l < current_price * 0.995]
    return len(below) <= 1
```

### 4.11 Объём как блокирующее условие

```python
avg_vol = mean(volume[-20:])
vol_ratio = volume[-1] / avg_vol

# Для подтверждения BOS/CHoCH (Pattern A шаг 6, Pattern B шаг 8):
vol_ok = vol_ratio >= 1.5
if not vol_ok:
    # пробой без объёма = ложный, не входим
    skip_signal()

# Для отмены:
vol_rejection = vol_ratio >= 2.0 and close_is_reversal
if vol_rejection:
    invalidate_signal()
```

---

## 5. Scoring — ранжирование паттернов

### 5.1 Score Pattern A

```python
score = 0.0
checks = 0

# Шаг 1: Импульс (обязательно для A1, опционально для A3)
if impulse_detected:
    score += 0.20
checks += 1

# Шаг 2: Поглощение
if absorption_detected:
    score += 0.25
    if one_candle_absorption:
        score += 0.05  # бонус
checks += 1

# Шаг 3: Боковик (боковик-1 или боковик без свипа)
if bokovik_detected:
    bonus = min(0.10, bokovik.touches * 0.02)
    score += 0.20 + bonus
checks += 1

# Шаг 4: Свип вниз
if sweep_below_detected:
    score += 0.20
checks += 1

# Шаг 4b: Второй боковик (после свипа)
if second_bokovik_detected:
    score += 0.10  # бонус за второй боковик
    checks += 0.5  # пол-шага

# Шаг 5: Слом структуры
if structure_break_up:
    score += 0.15
checks += 1

score = min(1.0, score / checks * 1.25)
```

### 5.2 Score Pattern B

```python
score = 0.0
checks = 0

# Шаг 1: HTF тренд
if htf_trend == "bull":
    score += 0.15
checks += 1

# Шаг 2: Финальный свип хая
if sweep_high_detected:
    score += 0.25
checks += 1

# Шаг 3: Нет ликвидности выше
if no_liquidity_above:
    score += 0.15
checks += 1

# Шаг 4: Затухание свечей
if candle_fade_detected:
    score += 0.20
checks += 1

# Шаг 5: LTF-подтверждение
if ltf_confirmation:
    score += 0.20
    if volume_confirmed:
        score += 0.05
checks += 1

score = min(1.0, score / checks * 1.25)
```

### 5.3 Пороги

| Score | Уровень | Действие |
|-------|---------|----------|
| ≥ 0.70 | Высокий | Полный вход. WS подписка на 1m. |
| 0.50-0.69 | Средний | Вход половинным лотом. WS подписка. |
| 0.30-0.49 | Низкий | Наблюдение. Нет WS. Нет входа. |
| < 0.30 | Нет | Паттерн не обнаружен. |

---

## 6. State machine (состояние на символ)

```python
class SymbolState:
    symbol: str
    pattern_type: "A" | "B" | "A2" | "A3" | None
    score: float = 0.0
    
    # События (каждое = {tf, timestamp, price, metadata})
    events: list[Event] = []
    
    # Прогресс по шагам
    steps_covered: int = 0  # 0..5
    total_steps: int = 0    # 5 для A/B
    
    # Детали
    impulse: Impulse | None = None
    absorption: Absorption | None = None
    bokovik: Bokovik | None = None          # боковик-1
    bokovik2: Bokovik | None = None         # боковик-2 (после свипа)
    sweep: Sweep | None = None
    structure_break: Break | None = None
    htf_trend: str = "neutral"
    candle_fade: Fade | None = None
    ltf_confirmation: Confirmation | None = None
    
    # WS подписка
    ws_subscribed: bool = False
    entry_tf_5m: list[Candle] = []  # агрегированные из 1m WS
    
    # Таймстемп последней проверки
    last_check: float = 0
```

### 6.1 Логика обновления state (вызывается каждый REST цикл)

```python
def update_state(state, bars_by_tf):
    # 1. Проверить шаги Pattern A
    if detect_impulse(bars_by_tf["4h"]):
        if not state.impulse:
            state.impulse = Impulse(...)
            state.steps_covered = max(state.steps_covered, 1)
    
    if state.impulse and detect_absorption(bars_by_tf["4h"]):
        if not state.absorption:
            state.absorption = Absorption(...)
            state.steps_covered = max(state.steps_covered, 2)
    
    if detect_bokovik(bars_by_tf["4h"]):
        if not state.bokovik:
            state.bokovik = Bokovik(...)
            state.steps_covered = max(state.steps_covered, 3)
        elif not state.sweep:
            state.bokovik = Bokovik(...)  # обновляем
        elif state.sweep and not state.bokovik2 and detect_bokovik(...):
            state.bokovik2 = Bokovik(...)
            state.steps_covered = max(state.steps_covered, 5)  # шаг 4b
    
    if detect_sweep_low(bars_by_tf["4h"], state.bokovik):
        if not state.sweep:
            state.sweep = Sweep(...)
            state.steps_covered = max(state.steps_covered, 4)
    
    if detect_bos_up(bars_by_tf["15m"]) or detect_choch_bull(bars_by_tf["15m"]):
        if not state.structure_break:
            state.structure_break = Break(...)
            state.steps_covered = max(state.steps_covered, 6)
    
    # 2. Проверить шаги Pattern B
    if detect_htf_trend_bull(bars_by_tf["1d"]):
        if not state.htf_trend:
            state.htf_trend = "bull"
            state.steps_covered = max(state.steps_covered, 1)
    
    if detect_sweep_high(bars_by_tf["4h"], macro_high):
        if not state.sweep:
            state.sweep = Sweep(...)
            state.steps_covered = max(state.steps_covered, 2)
    
    if no_liquidity_above and state.sweep:
        state.steps_covered = max(state.steps_covered, 3)
        # ВАЖНО: первый частичный вход можно делать здесь
        # (из транскрипции)
    
    if detect_candle_fade(bars_by_tf["4h"]):
        if not state.candle_fade:
            state.candle_fade = Fade(...)
            state.steps_covered = max(state.steps_covered, 4)
    
    if detect_bos_down(bars_by_tf["15m"]) or detect_choch_bear(bars_by_tf["15m"]):
        if not state.ltf_confirmation:
            state.ltf_confirmation = Confirmation(...)
            state.steps_covered = max(state.steps_covered, 5)
    
    # 3. Пересчитать score
    if state.impulse or state.htf_trend:
        state.score = compute_score(state)
```

### 6.2 Когда подключать WS и deliver

```python
if state.score >= 0.50 and not state.ws_subscribed:
    subscribe_1m_ws(state.symbol)
    state.ws_subscribed = True

if state.score >= 0.50 and state.steps_covered >= total_steps * 0.6:
    # Отправляем сигнал в Telegram
    deliver_signal(state)
```

---

## 7. Полная таблица констант

| Константа | Значение | Где |
|-----------|----------|-----|
| `MACRO_LOOKBACK` | 180 | 1d макро-экстремум |
| `MACRO_EXCLUDE` | 7 | Исключить последних 1d свечей |
| `SWEEP_MIN_PCT` | 0.5 | Мин. превышение макро-уровня для свипа |
| `MESO_CANDIDATES` | 12 | 4h свечей для проверки свипа |
| `BODY_ATR_MULT` | 1.5 | Множитель тела для импульса |
| `MOVE_ATR_MULT` | 1.0 | Множитель движения для серии |
| `MIN_CONSECUTIVE` | 3 | Мин. последовательных свечей |
| `ABSORB_RATIO` | 0.80 | Доля ретрейса для поглощения |
| `ONE_CANDLE_ABSORB` | 0.60 | Доля ≥60% диапазона для односвечного поглощения |
| `BOKOVIK_MIN_TOUCHES` | 3 | Мин. касание границ боковика |
| `BOKOVIK_MAX_WIDTH` | 15.0 | Макс. ширина боковика % |
| `BOKOVIK_ATR_RATIO` | 0.70 | Макс. ATR отношение для сжатия |
| `SWEEP_WICK_RATIO` | 0.30 | Мин. доля хвоста для свипа |
| `FADE_BODY_RATIO` | 0.50 | Порог body_ratio для затухания |
| `FADE_RANGE_RATIO` | 0.60 | Порог range_ratio для затухания |
| `FADE_LOOKBACK` | 8 | Окно для сравнения затухания |
| `BOS_BUFFER` | 0.003 | Буфер BOS/CHoCH |
| `SL_BUFFER` | 0.02 | Буфер стопа 2% |
| `ENTRY_BAND` | 0.002 | Ширина зоны входа 0.2% |
| `MIN_RR` | 1.2 | Мин. R:R |
| `VOL_CONFIRM_RATIO` | 1.5 | Объём для подтверждения BOS |
| `VOL_REJECT_RATIO` | 2.0 | Объём для отмены |
| `MIN_24H_VOLUME_USD` | 10_000_000 | Мин. объём для скрининга |
| `MAX_CHANGE_24H` | 80.0 | Макс. изменение для скрининга |
| `MIN_LISTING_DAYS` | 7 | Мин. возраст монеты |
| `SCAN_INTERVAL_S` | 900 | REST скрининг каждые 15 мин |
| `CONTEXT_INTERVAL_S` | 300 | REST контекст каждые 5 мин |
| `WS_ENTRY_TF` | "1m" | WS стрим для агрегации в 5m |
| `WS_ENTRY_TARGET_TF` | "5m" | Целевой ТФ входа из WS |
| `WS_ENTRY_MIN_BARS` | 12 | Мин. 5m свечей из WS перед входом |

---

## 8. Чем эта методика НЕ является

- **НЕ индикаторной**: нет RSI, MACD, EMA/SMA, BB
- **НЕ свечные паттерны**: doji, hammer, engulfing изолированно не используются
- **НЕ микроструктурой**: нет стакана, CVD, footprint, дельты
- **НЕ портфельной**: ничего не говорит про аллокацию, маржин, ликвидации

**Чем является**: детекцией цепочек событий на OHLCV через REST → WS с state machine на символ и Polars для batch-признаков.

---

*Документ является полной спецификацией. Каждая функция из разделов 4 и 6
должна быть имплементирована в scanner/detect/ как отдельный модуль.*
