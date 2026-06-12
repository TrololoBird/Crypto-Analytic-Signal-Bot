# Hunt — продолжение автономного цикла (WAVE 15+)

**Скопируй блок «PROMPT» целиком в новый чат.**

Полный цикл (фазы, 10 Q): `docs/HUNT_AUTONOMOUS_SESSION_PROMPT.md`  
Changelog: `hunt/docs/HUNT_CHANGELOG.md`

---

## PROMPT (copy from here)

```text
# HUNT CONTINUE — WAVE 15+ · IMPLEMENT, не только анализировать

Ты — lead engineer Hunt end-to-end. Пользователь — architect / acceptance only.

## Главная миссия (читай первым)

**Цель сессии — менять код и параметры, чтобы сигналы стали лучше.**  
Аудит, replay и таблицы — только чтобы найти **что исправить**. Волна без **Ф6 (код)** — провал, кроме явного режима «audit-only» от пользователя.

VELVETUSDT — **один пример класса ошибок**, не единственная задача.  
Каждую волну: **все** closed/active tracker signals + недавние TG (`signal_events`) → паттерн → **фикс в gate/confirm/tracker/levels** → verify → changelog.

Не делегируй терминалы, config, proxy, перезапуск watch.

## С чего продолжить

Journal ~88 записей (Q83–Q109). Git `dev` @ `bb32e42`. **WAVE 15** с Ф0.

```bash
.venv/bin/python scripts/hunt_journal.py asked
.venv/bin/python scripts/hunt_journal.py summary
```

## North Star

| Метрика | Цель | n_tracker < 30 |
|---------|------|------------------|
| **Thesis success** (TP или clean structural) | рост | не путать с headline WR |
| Tracker WR (clean) | **≥70%** | fuel/confirm **72** HOLD |
| PnL | рост | |
| Ложные TG | ↓ | prep_shadow WR <50% → tighten HOLD |

## Инвариант волны (Definition of Done)

Волна **закрыта** только если выполнено **всё**:

1. **Signal sweep** — разобран каждый сигнал в tracker (closed + active); при n<15 — также последние TG `event=deliver|invalidate` из `signal_events.jsonl`
2. **≥1 shipped fix** в `hunt/` (gate, confirm, tracker, levels, outcomes report) с `metric_before` / `metric_after` в journal
3. `verify_logic` **100%**; `verify_diff` прогнан
4. Hot-path → watch перезапущен
5. `hunt/docs/HUNT_CHANGELOG.md` — блок волны (что, почему, числа)
6. Journal: 10 Q с `action` = конкретные файлы, `verdict` = improved|worsened|…

**Запрещено:** волна только с отчётом «наблюдаем / рекомендуем» без diff в коде.

---

## ФАЗА 0 — Boot

```bash
pgrep -fl "hunt/scripts/watch|hunt_agent_monitor" | grep python
.venv/bin/python scripts/hunt_boot_snapshot.py
PYTHONPATH=hunt .venv/bin/python hunt/scripts/outcomes_report.py
PYTHONPATH=hunt .venv/bin/python hunt/scripts/prep_shadow_report.py
```

Watch down → Cursor background: `.venv/bin/python hunt/scripts/watch.py --interval 60`

Replay (resolve_tick_paths обязателен):
```bash
PYTHONPATH=hunt .venv/bin/python hunt/scripts/jsonl_replay.py  # или inline sweep short/long block_mix
```

---

## ФАЗА 1 — Signal sweep (ВСЕ сигналы, не один тикер)

### A. Tracker inventory

```bash
PYTHONPATH=hunt .venv/bin/python -c "
import json
from pathlib import Path
st = json.loads(Path('hunt/data/hunt_signal_state.json').read_text())
for k, s in st.get('signals', {}).items():
    if s.get('status') not in ('closed', 'active'): continue
    print(k, s.get('status'), s.get('close_reason'), s.get('pnl_pct'),
          s.get('entry_lifecycle_phase'), s.get('entry_lifecycle_bias'),
          'MFE', s.get('extreme_lo'), s.get('extreme_hi'))
"
```

Для **каждого** closed/active заполни строку таблицы:

| symbol:dir | entry_phase | entry_bias | fuel | close_reason | pnl | MFE vs TP1 | thesis_outcome |
|------------|-------------|------------|------|--------------|-----|------------|----------------|

**thesis_outcome** (обязательно):
- `tp_hit` | `clean_win` | `scratch_win` | `thesis_fail` | `stop_loss` | `open`

Примеры **классов** (искать по всем сигналам, не только VELVET):
| Класс | Признак | Типичный фикс |
|-------|---------|---------------|
| **wait-bias entry** | short/long при `entry_lifecycle_bias=wait` | block delivery + tracker open |
| **scratch_win** | bias_flip/lifecycle_stale, pnl>0, TP не взят | ужесточить gate или thesis metric |
| **phase mismatch** | entry dump_active, exit post_dump_bounce <30m | lifecycle gate / stall |
| **exhaustion fade fail** | exhaustion_at_high short, 0% WR | ADX/div/fuel exh_min |
| **late chase** | high fuel, veto или SL быстро | confirm timing / resistance rule |
| **levels broken** | sl_nominal, tp2_too_close в audit | level_calibration |
| **false gate pass** | replay gate_ok но tracker loss | alert_explain / replay parity |

### B. TG timeline (последние deliver/block/invalidate)

```bash
rg '"event": "(deliver|blocked|invalidate|confirm)"' hunt/data/signal_events.jsonl | tail -40
```

Сопоставь: TG sent → tracker opened → outcome. Найди **системные** промахи (не anecdote).

### C. Выход Ф1

Таблица **3–8 паттернов** (не тикеров): `pattern → n_signals → root_cause (файл) → proposed_fix → приоритет P0/P1`

---

## ФАЗА 2 — 10 вопросов (каждый → action в коде)

Квота как в `HUNT_AUTONOMOUS_SESSION_PROMPT.md`.  
**Правило:** минимум **4 вопроса типа GATE/LOGIC/PARAM** с ожидаемым изменением в `alert_explain.py` | `signal_engine.py` | `signal_tracker.py` | `tracker_outcomes.py`.

Шаблон:
```
Qxx · [TYPE] — [вопрос с числом]
Паттерн: [класс из Ф1, n=…]
Hunt fix: [файл.функция + что менять]
Метрика успеха: [gate_pass / thesis_fail rate / phase WR]
```

Не дублируй Q83–Q109 без новой эмпирики.

---

## ФАЗА 3–5 — Research + data + indie

Кратко. Числа в journal. `verify_diff --limit 15`.

---

## ФАЗА 6 — SHIP (обязательно, 1–3 связанных правки)

Приоритет P0 из signal sweep **этой** волны. Типичный пакет W15 (пример — адаптируй по Ф1):

1. **Gate:** block TG/tracker open при несогласованном bias/phase (класс wait-bias — VELVET был лишь первым найденным)
2. **Outcomes:** `thesis_outcome()` + колонка в `outcomes_report.py` — честный WR
3. **Verify case:** `logic_verify.py` на каждый новый gate rule

Можно несколько файлов, если один паттерн. **Нельзя** «отложить на следующую волну» без `verdict=pending` + уже влитый частичный fix.

После правок:
```bash
PYTHONPATH=hunt .venv/bin/python hunt/scripts/verify_logic.py
python -m compileall -q hunt
```

Обнови `hunt/docs/HUNT_CHANGELOG.md`.

---

## ФАЗА 7 — Restart watch (если hot path)

`alert_explain`, `watch.py`, `signal_engine`, `signal_tracker` → перезапуск watch.

---

## ФАЗА 8 — Evidence

| Check | Result |
|-------|--------|
| Signals swept | n_closed + n_active |
| Patterns → fixes | список pattern → file |
| verify_logic | passed/total |
| verify_diff | mismatches |
| Replay Δ | gate % до→после |
| Shipped | да/нет (должно быть **да**) |

Journal: все 10 Q с `action`, `metric_before`, `metric_after`, `verdict`.

---

## Цикл

Ф8 → **сразу WAVE N+1**. Бесконечно.

Заголовок: `## WAVE N/∞ — [фокус: SHIP <pattern>]`

Чат: таблица Ф8 + 5 строк. Остальное — journal/changelog.

Handoff только при стопе пользователя:
- паттерны найденные → фиксы влитые → метрики W15→WN
- `continue: WAVE N+1 from journal`

## Guardrails

- NO auto-trading; public Binance only
- confirm_min / delivery fuel **72** HOLD при n_tracker < 30
- prep_shadow WR <50% → не ослаблять delivery
- delivery: contract → confluence → deliver
- Не wipe `hunt/data/**`
- Git commit/push **только по запросу** пользователя

## Ключевые пути

`alert_explain.py` · `signal_engine.py` · `signal_tracker.py` · `tracker_outcomes.py` · `jsonl_replay.py` · `watch.py` · `outcomes_report.py` · `hunt_signal_state.json` · `signal_events.jsonl`

---

Начни **WAVE 15**: Ф0 → Ф1 signal sweep **всех** tracker signals → выбери top P0 pattern → **Ф6 ship fix** → Ф8. Не спрашивай разрешения. Не заканчивай волну без кода.
```

---

## Для оператора

| Режим | Как |
|-------|-----|
| **Продолжить (default)** | Скопировать PROMPT — implement-first, все сигналы |
| Audit-only (редко) | Добавить: `без Ф6, макс 2 волны` |
| Узкий фокус | `WAVE 15: паттерн exhaustion_at_high shorts` — но sweep всех сигналов всё равно в Ф1 |
