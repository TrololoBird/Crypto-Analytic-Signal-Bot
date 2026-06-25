# SCANNER_FINDINGS.md — P0/P1/P2 defect inventory

Date: 2026-06-24
Source: `SCANNER_FORENSIC.md` + full code audit of `hunt/hunt_core/scanner/detect/` and
`hunt/hunt_core/analysis/`

---

## P0 — Архитектурные дефекты, требующие исправления перед любыми изменениями

---

### P0.1 — Две параллельные системы фьюжн: одна рисует, другая решает

**Описание:** В системе работают два независимых скоринговых движка на каждый тик.
`analysis/manipulation_fusion.py` вычисляет archetype (predump/coil/ignition) с ~30
взвешенными факторами, а `scanner/detect/fusion.py` вычисляет signed-median magnitude
на основе 4 directional + 2 amplifier факторов. Оператор видит в логах и Telegram
archetype-лейблы и `primary_score`, но **решение о delivery принимает только второй
движок**.

**Риск:** Оператор отлаживает и калибрует не ту систему. Если archetype и fusion
расходятся (а они будут расходиться, так как используют разные входы и формулы),
объяснение сигнала в Telegram становится ложным. Пользователь видит "PREPUMP LONG
score=72" от manipulation_fusion, но реальное решение могло принять "short" на
основе signed-median.

**Файлы:**
- `hunt/hunt_core/analysis/manipulation_fusion.py:147-317` — вычисление archetype
- `hunt/hunt_core/scanner/detect/fusion.py:120-145` — signed-median fusion
- `hunt/hunt_core/scanner/detect/result.py:106-221` — build_detection вызывает Fz.fuse()
- `hunt/hunt_core/scanner/detect/delivery_setup.py:102-163` — p_win из fusion.fusion_score
- `hunt/hunt_core/scanner/gate/_policy_decl.py:287-292` — playbook gate skipped for pre_phase

**Доказательство (код):**

`result.py:123-125`:
```python
factors = compute_factors(window, row=context)
fusion = Fz.fuse(factors)
```

`delivery_setup.py:118-119`:
```python
"fusion_score": round(detection.fusion.fusion_score, 1),
"p_win": round(detection.fusion.fusion_score / 100.0, 4),
```

`manipulation_fusion.py:345-350` — stamp_fusion_on_row пишет в `row["manipulation_fusion"]`,
но этот dict не читается обратно в build_detection или build_delivery_setup.

**Как проверить:** Добавить в build_detection логирование, которое сравнивает
`fusion.side` и `fusion_score` с `manipulation_fusion.archetype` и `primary_score`.
Запустить на 100 тиках — подсчитать процент расхождений.

**Как исправить:** Один из двух вариантов:
- (a) Удалить manipulation_fusion из hot path, оставить только для deep-панелей.
  Delivery_setup должен читать факты (archetype, scores) из Detection, не из
  отдельного анализа.
- (b) Сделать manipulation_fusion.authoritative для pre_phase: delivery_setup
  должен брать side/score/confidence из archetype-системы, а signed-median fusion
  оставить как запасной/fallback.

---

### P0.2 — p_win = magnitude × 0.25, а не калиброванная вероятность

**Описание:** `p_win` в delivery_setup вычисляется как `fusion_score / 100`, где
`fusion_score = min(100, magnitude × 25)`. То есть `p_win = magnitude × 0.25` (capped).
`magnitude` — это `|z_dir| × (1 + amp)`, где z_dir — signed median of robust-z scores.
Никакой привязки к реальной частоте выигрышей нет. Код сам пишет
`"NOT calibrated P(win)"` в докстринге FusionScore.

**Риск:** Вся система управления рисками (размер позиции, фильтр по confidence,
сравнение сигналов) опирается на `p_win`, который не означает "вероятность успеха".
P(win)=63% не означает, что 63 из 100 таких сигналов будут прибыльными. Это просто
нормированная сила сигнала. Risk-reward расчёт в `compute_setup_risk_reward`
использует `p_win` — все `RR` значения в Telegram потенциально бессмысленны.

**Файлы:**
- `hunt/hunt_core/scanner/detect/fusion.py:49-52` — magnitude_to_fusion_score
- `hunt/hunt_core/scanner/detect/fusion.py:55-72` — FusionScore dataclass
- `hunt/hunt_core/scanner/detect/delivery_setup.py:118-119` — p_win присваивание
- `hunt/hunt_core/scanner/detect/config.py:30` — fusion_score_scale = 25.0
- `hunt/hunt_core/shared/contract.py` — compute_setup_risk_reward (consumer)

**Доказательство (код):**

`fusion.py:49-52`:
```python
def magnitude_to_fusion_score(magnitude: float) -> float:
    if not math.isfinite(magnitude):
        return 0.0
    return min(100.0, max(0.0, magnitude * _fp().fusion_score_scale))
```

`delivery_setup.py:119`:
```python
"p_win": round(detection.fusion.fusion_score / 100.0, 4),
```

**Как проверить:** Собрать 100+ executed outcomes, для каждого записать
`fusion_score`. Разбить на децили по fusion_score. Вычислить realized win-rate в
каждом дециле. Если P(win) ≠ realized win-rate — p_win фиктивен. Ожидается, что
реализованная частота будет шумной и не совпадёт с magnitude × 0.25.

**Как исправить:**
- (a) Пока данных о outcomes мало (<200): явно переименовать `p_win` в `fusion_score`
  во всех точках потребления (delivery, templates, dashboard), убрать деление на 100.
  Downstream должен знать, что это strength index, не вероятность.
- (b) После накопления данных: построить калибровочную таблицу
  `fusion_score_decile → realized_win_rate` и заменить формулу на эмпирическую.

---

### P0.3 — Gate открывается, когда "необычно для данного символа", а не "похоже на пре-памп"

**Описание:** `fusion.py::gate()` вычисляет порог как `max(q90 собственной истории
magnitude символа, global_floor)`. Это self-referential quantile: gate открывается
для ~8% баров каждого символа (q=0.92), независимо от того, предшествует ли такое
состояние реальному пампу/дампу.

**Риск:** Сканер может чаще выбирать тихие монеты (где любое движение "необычно") и
пропускать волатильные монеты (где движение 5% — норма). Gate ничего не знает о
том, предшествует ли данное состояние будущему движению — он знает только, что оно
необычно для данного символа. Связь "необычность ↔ предсказательная сила" — это
hypothesis, а не proven fact.

**Файлы:**
- `hunt/hunt_core/scanner/detect/fusion.py:152-186` — gate()
- `hunt/hunt_core/scanner/detect/config.py:22-23` — q_gate = 0.92
- `hunt/hunt_core/scanner/detect/calibrate.py:101-110` — quantile_gate
- `hunt/hunt_core/shared/mathlib/stats.py:55-60` — quantile()

**Доказательство (код):**

`fusion.py:177`:
```python
sym_threshold = C.quantile_gate(magnitude_history, q, min_n=min_n)
```

`fusion.py:181`:
```python
effective = max(sym_threshold, global_floor)
if adj_mag < effective:
    return GateDecision(False, ..., f"below_calibrated_gate")
return GateDecision(True, ..., f"gate_open")
```

Пример: монета А с median magnitude=2.0, q90=4.0 → gate открывается при magnitude≥4.0.
Монета Б с median=0.5, q90=1.2 → gate открывается при magnitude≥1.2.
Монета Б может сигналить при magnitude=1.2, хотя монета А не сигналит при 3.5.

**Как проверить:**
- Запустить live_watch, собрать histogram magnitude для каждого символа.
- Вычислить per-symbol firing rate: сколько % баров открывают gate.
- Если rate ~uniform(8%) для всех символов — gate ничего не разделяет.
- Собрать outcomes, разбить на "gate_open → profit" vs "gate_closed → profit".
  Если AUC < 0.55 — gate не имеет предсказательной силы.

**Как исправить:**
- Заменить self-referential quantile на outcome-derived threshold: какие magnitude
  historically precede profitable setups для каждого символа.
- Или, как минимальное исправление, добавить global_gate_floor (уже есть = 0.55,
  но он ниже q90 для многих символов — не помогает).

---

### P0.4 — "Pre-pump" archetype требует признаков уже начавшегося пампа

**Описание:** В `manipulation_fusion.py` archetype `prepump_long` требует (среди
5/7) `vah_break_5m` (close > 5m VAH) и `vol_above_median_5m` (vol_ratio ≥ 1.5×).
Оба этих условия означают, что пробой уже произошёл и объём уже пришёл. Это
early-pump, не pre-pump.

В live fusion engine проблема глубже: directional фактор `structure` использует
**mean-reversion** (функция возвращает `-stretch`). Если RSI, BB %b или z-score
высоки (что типично для начала импульса), `structure` голосует SHORT. Это
adversarial для pre-pump LONG детектора.

**Риск:**
- `vah_break_5m` + `vol_above_median_5m` в required checks означают, что
  оператор видит "pre-pump" на сигнале, где памп уже начался — это late entry.
- `structure` как mean-reversion на pump-детекторе: 1 из 4 directional факторов
  систематически голосует против направления, которое детектор ищет.
  `median(book=+2.0, flow=+1.5, structure=-1.5, funding=+0.5)` = 1.0 → long,
  но если structure=-2.5, то median(book=+2.0, structure=-2.5, flow=+1.5,
  funding=+0.5) = 1.0 → всё ещё long, но magnitude снижен.

**Файлы:**
- `hunt/hunt_core/analysis/playbook_checks.py:17-26` — prepump_long required checks
- `hunt/hunt_core/analysis/manipulation_fusion.py:125-144` — vah_break + vol checks
- `hunt/hunt_core/scanner/detect/factors.py:73-86` — structure factor mean-reversion

**Доказательство (код):**

`factors.py:84-85`:
```python
score = -stretch  # mean reversion: positive stretch ⇒ short pressure
```

`manipulation_fusion.py:235-240`:
```python
if _apply_check(checks, check_sources, "vah_break_5m", vah_break, "coinxsight"):
    coil += 8.0
if _apply_check(checks, check_sources, "vol_above_median_5m", vol_relative, "coinxsight"):
    coil += 8.0
```

**Как проверить:**
- Для live fusion: собрать distribution z-scores для `structure` на pre-pump long
  сигналах. Если median(structure.score) < 0 на pre-pump long сигналах — фактор
  adversarial.
- Для manipulation_fusion: проверить, сколько pre-pump delivery-сигналов имели
  `vah_break_5m == True` — ожидается >70% (так как он в required checks).

**Как исправить:**
- (a) Для `structure`: удалить или перевернуть знак на pump-детекторе.
  Заменить `score = -stretch` на `score = stretch` для pre-pump сценария, или
  выделить отдельные коэффициенты для long/short.
- (b) Для `manipulation_fusion`: убрать `vah_break_5m` и `vol_above_median_5m`
  из required checks prepump_long, или переименовать archetype в "early_pump".

---

## P1 — Серьёзные дефекты, влияющие на достоверность сигналов

---

### P1.1 — Веса в manipulation_fusion не влияют на решение

**Описание:** Веса (22, 18, 16, 14, 12, 10, 8…) присваиваются per-check в
`manipulation_fusion.py`, но `primary_score` вычисляется в `playbook_checks.py`
как `100 × pass_count / len(required_keys)` — невзвешенное отношение. Веса
используются только для `score_predump/coil/ignition`, которые идут в
`deep/fusion_panel.py` на отображение, а не в решение.

**Риск:** Калибровка весов (изменение 22→20, 14→16 и т.д.) НЕ влияет на то,
какие сигналы проходят. Оператор может тратить время на тонкую настройку
параметров, которые ничего не меняют. Более того, `playbook_pass_count` не
использует веса — решение принимается по принципу "прошло N из M", где N и M
жёстко заданы (4/6, 5/7, 5/5).

**Файлы:**
- `hunt/hunt_core/analysis/manipulation_fusion.py:164-211` — predump scores с весами
- `hunt/hunt_core/analysis/playbook_checks.py:86-107` — best_archetype_by_ratio
- `hunt/hunt_core/analysis/playbook_checks.py:49-64` — playbook_pass_count

**Доказательство (код):**

`playbook_checks.py:96-98`:
```python
pc = sum(1 for k in keys if checks.get(k))  # pass_count — без весов
total = len(keys)
ratio = 100.0 * pc / total
```

**Как проверить:** Изменить вес в `manipulation_fusion.py` (например, 22→1 для
distribution_phase). Перезапустить. Результаты `primary_score` не изменятся.
(Изменится только отображаемый `score_predump`.)

**Как исправить:**
- (a) Убрать веса из manipulation_fusion, оставить только pass_count/required_n.
  Удалить `score_predump/coil/ignition` и связанные расчёты.
- (b) Или: сделать primary_score взвешенным: `sum(check.weight for passed checks)`.
  Это потребует рекалибровки N-of-M threshold для каждого archetype.

---

### P1.2 — structure factor adversarial на pre-pump long

(Описано в P0.4 как часть проблемы. Выделено отдельно, так как это ошибка в
fusion engine, а не в manipulation_fusion, и исправляется независимо.)

---

### P1.3 — pre_phase_gate использует фиксированные константы вместо калиброванных

**Описание:** `pre_phase_gate` в `fusion.py` использует три жёстких порога:
`PRE_GATE_MIN_ENERGY = 3`, `PRE_GATE_MIN_STRUCTURE = 0.18`,
`PRE_GATE_MIN_MAGNITUDE = 0.15`. В отличие от momentum gate, эти пороги не
self-calibrated и не outcome-derived. Они выбраны как "reasonable defaults"
без эмпирического обоснования.

**Риск:** Пороги могут быть неправильными для разных символов. Для волатильных
монет magnitude=0.15 — это шум, для тихих — норма. Структурный порог 0.18 для
book imbalance не основан на исторических данных.

**Файлы:**
- `hunt/hunt_core/scanner/detect/fusion.py:97-101` — PRE_GATE_MIN константы
- `hunt/hunt_core/scanner/detect/fusion.py:104-117` — pre_phase_gate

**Как проверить:** Собрать distribution depth_imbalance для символов с разной
волатильностью. Проверить, какой процентиль соответствует 0.18. Если процентиль
сильно различается между символами — порог должен быть per-symbol.

**Как исправить:** Заменить константы на per-symbol quantile thresholds, как в
momentum gate. `structure_score` → `quantile(depth_imbalance_history, q)`.

---

### P1.4 — CUSUM threshold фиксирован per-config

**Описание:** В `phase.py` порог CUSUM вычисляется как `fp.cusum_k * 4.0`
(cusum_k = 0.5 → threshold = 2.0). Это одинаково для всех символов, хотя
разные символы имеют разную волатильность standardized returns.

**Риск:** Для тихих символов threshold может быть слишком высоким (MID не
активируется никогда → pre_pump застревает "watch_ok=True" вечно, так как
никогда не видит MID). Для волатильных — слишком низким (ранний MID →
pre-фаза обрывается до того, как сигнал сформирован).

**Файлы:**
- `hunt/hunt_core/scanner/detect/phase.py:73` — cusum_threshold
- `hunt/hunt_core/scanner/detect/config.py:32-33` — cusum_k=0.5, cusum_span=96

**Как проверить:** Собрать cusum_series для 10 символов разной волатильности.
Вычислить q90(|cusum|) per symbol. Если q90 сильно различается — threshold
должен быть per-symbol.

**Как исправить:** Сделать CUSUM threshold self-calibrated per symbol:
`threshold = quantile_gate(|cusum_series|, q=0.85)`.

---

## P2 — Умеренные дефекты и архитектурные замечания

---

### P2.1 — magnitude_history_for_frame не обрезает историю

**Описание:** `_cache[key] = (height, mags)` накапливает magnitude для каждого
бара без ограничения по lookback. Никакого tail-обрезания нет — для символа,
работающего неделями на 15m, список вырастет до тысяч значений. `quantile_gate`
всё равно считает np.quantile по всему массиву, но это меняет поведение gate:
ранние бары влияют на q90 навсегда.

**Риск:** Если у символа был период высокой волатильности месяц назад, его q90
будет завышен навсегда. Gate will require higher magnitude для открытия, чем
нужно для текущего режима.

**Файлы:**
- `hunt/hunt_core/scanner/detect/magnitude_cache.py:39-53`

**Как исправить:** Добавить tail(lookback) на mags перед возвратом, или
использовать кольцевой буфер ограниченной длины.

---

### P2.2 — Display-only smart-money checks не гейтят, но показаны как значимые

**Описание:** `SMART_MONEY_DISPLAY_CHECKS = frozenset({"vol_oi_sane",
"flow_aligned"})` явно исключены из N-of-M required sets, но вычисляются и
отображаются в deep-панелях. Оператор может думать, что они влияют на решение.

**Риск:** Когнитивная нагрузка — оператор видит 9-12 checks, но только 5-7
из них реально что-то решают. Два checks показаны, но не имеют веса.

**Файлы:**
- `hunt/hunt_core/analysis/playbook_checks.py:46`

**Как исправить:** Или сделать их гейтящими, или убрать из display в deep-
панелях, или явно пометить "(info only)" в UI/Telegram.

---

### P2.3 — Squeeze double-counted

**Описание:** `_squeeze_blocks_predump` применяется дважды:
1. В `manipulation_fusion.py:206` как `predump *= 0.35` (score penalty)
2. В gate stack как hard gate `squeeze_predump` (см. SCANNER_FORENSIC.md §5)

**Риск:** Сигнал, прошедший score penalty и hard gate, отфильтрован дважды
одним и тем же условием. Если squeeze_block=True, predump score падает на 65%,
а затем gate всё равно может заблокировать delivery.

**Файлы:**
- `hunt/hunt_core/analysis/manipulation_fusion.py:203-207`
- `hunt/hunt_core/scanner/gate/_registry.py` — gate registration

**Как исправить:** Убрать squeeze из gate stack или из score penalty, оставить
только одну точку применения.

---

### P2.4 — Вероятность структуры self-referential gate ≈ константа

**Описание:** С q_gate=0.92, ~8% баров каждого символа будут открывать
momentum gate. Для символа с 96 барами/день (15m) = ~7.7 открытий gate в день.
Это означает, что сканер будет "сигналить" каждому символу ~8 раз в день
просто по статистике, до применения любых других фильтров (mission, pre-фаза,
манипуляция).

**Риск:** Базовая частота срабатываний предсказуема и не отражает реальной
частоты событий. Если pre-фаза длится ~30% времени, то effective firing rate
может быть ~2.4 сигнала в день на символ только от gate.

**Файлы:**
- `hunt/hunt_core/scanner/detect/config.py:23` — q_gate = 0.92

**Как проверить:** Собрать live_watch stat: ticks per symbol per day, gate_open
rate, pre_phase rate. Gate_open should be ~8%, pre_phase ~30% → effective
pre_phase deliveries = ~2.4% of ticks.

---

### P2.5 — Факторы book и flow не могут быть backtested

**Описание:** `factor_book` использует `depth_imbalance` и `microprice_bias`
(orderbook-derived). `factor_flow` использует `delta_ratio` и `rolling_cvd_24h`
(taker flow). Для этих данных нет исторического архива — только live stream.
Replay-тесты используют thin lake (без orderbook колонок) → эти факторы
abstain при реплее.

**Риск:** Решение в production использует 2 из 4 directional факторов, которые
невозможно проверить на истории. Если book и flow ошибаются (дают ложные
сигналы), это проявится только в live_watch. Backtest показывает только
поведение structure + funding (2 фактора), что может сильно отличаться от
поведения всех 4.

**Файлы:**
- `hunt/hunt_core/scanner/detect/factors.py:63-70` — factor_book
- `hunt/hunt_core/scanner/detect/factors.py:101-116` — factor_flow

**Как исправить:** Вести persistent outcome tracker для book/flow зависимых
сигналов. Отдельно считать win-rate для "book+flow только" vs "structure+funding
только" vs "все 4". После 50+ outcomes будет понятно, улучшают ли book/flow
результат.

---

## Сводка

| ID | Severity | Файл(ы) | Суть |
|---|---|---|---|
| P0.1 | Критический | `detect/fusion.py`, `analysis/manipulation_fusion.py` | Две системы: одна рисует, другая решает |
| P0.2 | Критический | `detect/fusion.py:49-52`, `delivery_setup.py:119` | p_win = magnitude × 0.25, не вероятность |
| P0.3 | Критический | `detect/fusion.py:152-186` | Gate по q90 себя, не по предиктивности |
| P0.4 | Критический | `playbook_checks.py:17-26`, `factors.py:84-85` | Pre-pump = early-pump, structure adversarial |
| P1.1 | Высокий | `analysis/manipulation_fusion.py:164-211`, `playbook_checks.py:86-107` | Веса не влияют на primary_score |
| P1.3 | Высокий | `detect/fusion.py:97-101` | pre_phase_gate константы не калиброваны |
| P1.4 | Высокий | `detect/phase.py:73` | CUSUM threshold фиксирован |
| P2.1 | Средний | `detect/magnitude_cache.py:39-53` | Нет обрезания истории magnitude |
| P2.2 | Средний | `playbook_checks.py:46` | Display-only checks без влияния |
| P2.3 | Средний | `manipulation_fusion.py:203-207`, `gate/_registry.py` | Squeeze double-counted |
| P2.4 | Средний | `detect/config.py:23` | Gate firing rate ≈ константа |
| P2.5 | Средний | `detect/factors.py:63-70, 101-116` | book/flow не backtestable |
