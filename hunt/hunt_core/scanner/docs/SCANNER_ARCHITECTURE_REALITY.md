# SCANNER_ARCHITECTURE_REALITY.md — что такое Scanner сегодня

Источник: полный trace call chain от тика до delivery emit
(`dispatch.py:211 → gate_pipeline → arbiter → telegram`).

---

## 1. Полная цепочка принятия решения

```
tick → _evaluate_auto_delivery()
  → evaluate_delivery()                              [deliver/dispatch.py:211]
      1. run_gate_pipeline()                          [dispatch.py:237]
         → scanner/gate/ (builtin gates + _policy_decl)
      2. stamp_fusion_on_row(row)                     [dispatch.py:247-250] ← если отсутствует
         → analysis/manipulation_fusion.py            [ВЫЧИСЛЕНИЕ данных]
      3. route_delivery_lane()                        [dispatch.py:252]
         если lab_lane → contract check → return      [dispatch.py:255-265]
      4. evaluate_confirm_authorities()                [dispatch.py:267]
         → scanner/delivery/arbiter.py                [ФИНАЛЬНОЕ РЕШЕНИЕ]
      5. contract check (R:R, levels)                 [dispatch.py:279]
  → build_scanner_signal() + format_telegram_confirm()
  → send_lane_html()                                  [ОТПРАВКА в TG]
```

---

## 2. Таблица: что решает, что показывает

### Для pre_phase (основная миссия Scanner)

| Компонент | Решает? | Показывает? | Файл:строка |
|---|---|---|---|
| `detect/fusion.py:fuse()` | **ВЫЧИСЛЕНИЕ** median(z_dir), magnitude | — | `scanner/detect/fusion.py:120-145` |
| `detect/fusion.py:gate()` | **ДА** — self-referential quantile gate | — | `scanner/detect/fusion.py:152-186` |
| `detect/fusion.py:pre_phase_gate()` | **ДА** — structure-based gate (bypass magnitude floor) | — | `scanner/detect/fusion.py:104-117` |
| `detect/factors.py:*` | **ВЫЧИСЛЕНИЕ** factor scores → fuse | — | `scanner/detect/factors.py:63-116` |
| `detect/phase.py:assess_phase()` | **ДА** — watch_ok (pre vs mid) | — | `scanner/detect/phase.py:53-105` |
| `detect/result.py:build_detection()` | **ДА** — сборка Detection, gate_open, pre_gate_open, signal_type | — | `scanner/detect/result.py:106-221` |
| `gate/_mission.py:mission_delivery_block()` | **ДА** — блокирует mid-leg / late-chase | — | `scanner/gate/_mission.py:155-283` |
| `gate/_policy_decl.py:playbook` | **НЕТ** — **skip для pre_phase** (строка 288) | — | `scanner/gate/_policy_decl.py:287-292` |
| `delivery/arbiter.py` | **ДА** — fusion_gate_open + mission_pass | — | `scanner/delivery/arbiter.py:10-60` |
| `arbiter.py:playbook` | **НЕТ** — **skip для pre_phase** (строки 27, 48) | — | `scanner/delivery/arbiter.py:26-27,47-48` |
| `analysis/manipulation_fusion.py` | **ВЫЧИСЛЕНИЕ** archetype, scores, playbook | — | `analysis/manipulation_fusion.py:147-317` |
| `deep/fusion_panel.py` | — | **ДА** — archetype, scores, checks в deep panel | `deep/fusion_panel.py:7-37` |
| `scanner/telegram.py` | — | **ДА** — форматирование TG сообщения | `scanner/telegram.py:82-130` |

### Для mid_phase (non-pre-phase)

| Компонент | Решает? | Показывает? |
|---|---|---|
| `detect/fusion.py:gate()` | **ДА** | — |
| `gate/_policy_decl.py:playbook` | **ДА** — N-of-M блокирует delivery | — |
| `arbiter.py:playbook` | **ДА** — playbook_pass_ok обязателен | — |
| `analysis/manipulation_fusion.py` | **ВЫЧИСЛЕНИЕ** playbook N-of-M | — |
| Весь display | — | **ДА** |

---

## 3. Ключевой вывод

### Для pre_phase (основная миссия):

```
РЕШАЕТ:   detect/fusion.py  (signed-median robust-z magnitude)
ПОКАЗЫВАЕТ: manipulation_fusion.py (archetype checklist с весами 22/18/16…)
```

`manipulation_fusion` — **чистое отображение** для pre_phase.
Оператор видит `absorption`, `accumulation`, `coil` — но решение принял
`median(book, flow, structure, funding)`.

### Для mid_phase:

```
РЕШАЕТ:   detect/fusion.py AND manipulation_fusion (playbook N-of-M)
```

Обе системы должны пропустить сигнал. Двойной контроль для уже
начавшегося движения.

---

## 4. Почему так получилось — реконструкция эволюции

Судя по коду, Scanner прошёл через следующие поколения:

**Gen 1 (оригинал):** `manipulation_fusion.py` — archetype checklist,
взвешенная сумма, playbook N-of-M. Был единственным решением.

**Gen 2 (рефакторинг):** `scanner/detect/fusion.py` — statistical fusion
на robust-z. Добавлен как замена или дополнение.

**Gen 3 (компромисс):** Gate stack + arbiter — два слоя проверок,
каждый со своим исключением для pre_phase.

Результат:

```
manipulation_fusion остался как:
  - compute (для deep_panel, outcome_ledger)
  - decision (только для mid_phase)
  - display (всегда, включая pre_phase)

detect/fusion стал:
  - decision (pre_phase + mid_phase)
```

Старый слой не удалён, а обёрнут новым. Для pre_phase старый слой
**живёт как декорация**.

---

## 5. Что это значит для пользователя

Когда Scanner отправляет pre_phase сигнал, Telegram показывает:

```
🧬 Manipulation fusion
Archetype: prepump_long
Scores — predump 0 coil 72 ignition 0
Playbook: 5/7 checks
Checks: coil_phase, vp_accumulation, va_contraction, bid_absorption, ...
```

**Но реально сигнал появился из-за:**

```python
z_dir = median(book.z(=+1.8), flow.z(=+1.2), structure.z(=-0.3), funding.z(=+0.7))
# z_dir = 0.95 (long)
magnitude = 0.95 * (1 + tanh(0.8)) = 1.5
fusion_score = min(100, 1.5 * 25) = 37.5
# gate: 1.5/atr_pct > q90(magnitude_history)?
# если да → gate_open
```

Эти два объяснения могут расходиться полностью:
- `manipulation_fusion` говорит "accumulation + coil + absorption"
- `detect/fusion` говорит "microprice_bias + CVD_slope - stretch"

И оператор не видит второго.

---

## 6. Калибровочный аспект

Если калибровать `manipulation_fusion` (менять веса 22→20, thresholds),
это НЕ повлияет на pre_phase delivery вообще. Изменения увидят только
deep panel и Telegram display. Решение останется тем же.

Если калибровать `detect/fusion` (менять `q_gate`, `fusion_score_scale`,
`robust_z_clip`) — это повлияет на pre_phase delivery, но оператор
увидит в логах неизменные archetype-показатели.

---

## 7. Что должно произойти

До перехода к Deep Forensic нужно ответить на вопрос:

```
Какая система должна быть авторитетной для pre_phase?
```

Вариант А: `manipulation_fusion` становится авторитетной
- delivery_setup берёт side/confidence из archetype
- detect/fusion становится fallback или display
- playbook N-of-M начинает гейтить pre_phase (сейчас он skip)
- нужно: веса → решение (сейчас dead), плюс P0.4 (vah_break в pre)

Вариант Б: `detect/fusion` остаётся авторитетной
- manipulation_fusion удаляется из hot path (только deep panel)
- Telegram перестаёт показывать archetype-объяснение
- или Telegram показывает detect/fusion factor scores вместо archetype
- нужно: P0.3 (self-referential gate)

Вариант В: гибрид — новая unified scoring система
- Объединить оба подхода в один dataclass/функцию
- Единый pipeline: factors → unified_score → gate
- manipulation_fusion уходит в архив
