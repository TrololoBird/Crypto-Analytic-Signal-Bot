# Hunter (Охотник) — Product Definition (Track 1)

> **Статус:** **G2 ЗАКРЫТ** — выбран **H-B «Широкий хантер»** (2026-06-12).  
> **G1:** полный rewrite ядра → `hunt_core/` (см. [HUNT_REWRITE_MIGRATION.md](HUNT_REWRITE_MIGRATION.md)).  
> **Дата:** 2026-06-12  
> **Основа:** [HUNT_TRUTH_AUDIT.md](HUNT_TRUTH_AUDIT.md) + воспроизводимый анализ `hunt/data/*`.  
> **Метод:** hold-to-target grading (`bt_outcome`) как честная метрика; live `thesis_success`
> используется только с пометкой «inflated».

---

## 1. Что данные говорят про нишу

Охотник фактически торгует **одну проверенную нишу**:

> **Short fade активного дампа memecoin** — вход после pump, в фазе `dump_active`, только при
> прохождении confirm-gate.

| Факт | Доказательство |
|------|----------------|
| 100% live closes — short | `closed_history` n=13, `signal_history` n=12 |
| 12/13 live — фаза `dump_active` | `closed_history` |
| 0 long live closes | ни разу не доставлен/не закрыт long в production |
| Confirm-gate = edge | gate_edge short SL **27%** vs raw **52%** (n=135) |
| Лучший бакет — `dump_active` short | gate_edge n=37: SL **19%**, TP1+ **68%** |
| Long confirm — слабый | gate_edge long n=50: SL **34%**, TP1 **22%** |
| Сырой fade-universe — loser | enriched backtest SL **52.4%** (n=252) |
| Ранний выход завышает live WR | `early_exit_verdict` n=5: forfeited 2× TP2, avoided 0 stops |

**Вывод:** продуктовая ниша уже проявилась в коде и данных, но не зафиксирована в доках.
Система пытается быть «short+long × 8 фаз», а edge есть у **одного среза**.

---

## 2. Edge map — где winners vs losers

### 2.1 Источники данных

| Источник | n | Метрика | Роль |
|----------|---|---------|------|
| `gate_edge_outcomes.jsonl` | 185 | hold-to-target на **confirmed** ticks | **главный edge-доказатель** |
| `backtest_outcomes_enriched.jsonl` | 252 | hold-to-target на raw+synthetic legs | baseline / anti-loosen guard |
| `backtest_outcomes.jsonl` | 265 | то же (частично без ATR) | legacy grade |
| `signal_history.jsonl` | 12 | live closes + feature_latch | крошечная валидация UX/tracking |
| `prep_shadow_events.jsonl` | 1 148 closed | paper direction WR | воронка до TG, не PnL truth |
| `signal_events.jsonl` | 47 156 | forming/blocked/confirmed | воронка детекции |

Команды воспроизведения — в §7.

### 2.2 Направление (gate_edge, hold-to-target)

| Направление | n | SL | TP1+ | TP2 | Вердикт |
|-------------|---|-----|------|-----|---------|
| **short confirmed** | 135 | **27%** | **50%** | 35% | ✅ edge vs 52% raw |
| long confirmed | 50 | 34% | 22% | 18% | ⚠️ marginal; хуже short |

### 2.3 Фаза lifecycle (gate_edge short, n≥8)

| Фаза | n | SL | TP1+ | TP2 | Вердикт |
|------|---|-----|------|-----|---------|
| **dump_active** | 37 | **19%** | **68%** | 43% | ✅ **лучший срез** |
| unknown/прочие | 87 | 30% | 45% | 32% | средний |
| long × фаза | 49 | 35% | 20% | 16% | ❌ не приоритет |

### 2.4 Fuel bucket (gate_edge short)

| Fuel | n | SL | TP1+ |
|------|---|-----|------|
| fuel96+ | 11 | 18% | 73% |
| fuel64-79 | 27 | 22% | 59% |
| fuel80-95 | 31 | 29% | 48% |
| unknown | 65 | 29% | 43% |

**Паттерн:** edge не монотонен по fuel; средний fuel 64–96 достаточен. Высокий fuel ≠
автоматически лучший исход (противоречит старому тезису «fuel 80–95 WR 26%» из CHANGELOG —
тот был live-thesis, не hold-to-target).

### 2.5 Символы (gate_edge short, n≥3)

| Tier | Примеры | SL | n | Комментарий |
|------|---------|-----|---|-------------|
| Сильные | PLAYUSDT, LABUSDT, SIRENUSDT, ESPORTSUSDT | 0% | 3–7 | малый n |
| Средние | BTWUSDT, UBUSDT, SPACEUSDT, XAUUSDT | 17–33% | 3–6 | |
| Слабые | VELVETUSDT, JCTUSDT, HMSTRUSDT, STGUSDT | 50–67% | 3–10 | symbol-risk |

**Риск:** edge агрегирован по memecoin-universe; per-symbol n<10 — не для автоматического
symbol-filter без нового n≥30 цикла.

### 2.6 Prep-shadow (paper, без TG)

| Срез | n | Direction WR | Комментарий |
|------|---|--------------|-------------|
| **dump_active** | 14 | **57.1%** | совпадает с gate_edge лидером, но n мало |
| distribution | 26 | 46.2% | ниже порога |
| exhaustion_at_high | 80 | 31.2% | слабый |
| impulse_initiating | 67 | 26.9% | слабый |
| tier `start` | 44 | 43.2% | лучше `prep` (29.5%) |
| **confirm funnel** | — | **1.5%** | из 1 304 opened → 46 TG confirmed |

### 2.7 Live tracking (n=13–19, не для edge-claims)

| Метрика | Значение | Почему не North Star |
|---------|----------|---------------------|
| thesis_success | 89% (17/19) | lifecycle_stale → scratch_win |
| tp_hit | 16% (3/19) | честная цель |
| stop_hit (raw) | 3–5 | есть реальные стопы |
| early_exit vs hold | forfeited 2 TP2 / 0 avoided stops | net-negative |

Live PnL mean **+7.3%** (archive n=12) — **не воспроизводим** на hold-to-target; ESPORTS
tp2 +36% тянет среднее при n<15.

### 2.8 Воронка блокировок (signal_events, n=1 333 blocked)

| block_code | count | Интерпретация |
|------------|-------|---------------|
| `short_entry_not_ok` | 705 | lifecycle говорит «рано для short» |
| `below_forming_min` | 269 | score/fuel ниже порога |
| `prep_shadow_tighten` | 156 | калибровка ужесточает по слабому shadow |
| `filter_block` | 81 | directional/regime filters |
| `delivery_confluence_low` | 26 | confluence gate |

**Паттерн:** система уже **узкая** на практике (мало confirmed); проблема — не отсутствие
фильтров, а размытый продуктовый фокус и раздутая метрика успеха.

---

## 3. Три продуктовые гипотезы (G2)

### H-A — «Снайпер» (рекомендация исполнителя по данным)

**Определение:** signal-only Telegram-алерты **только short fade** в фазе `dump_active` после
confirm-gate. Long отключён осознанно до n≥30 валидации отдельным треком.

| Параметр | Значение |
|----------|----------|
| Universe | memecoin USD-M, vol+impulse screener (текущий) |
| Направление | short only |
| Фаза | `dump_active` (hard filter) |
| Confirm | существующий `confirm_dump` + structural hard triggers |
| Delivery | Telegram + tracker; **без** ignition/early_alert TG (или advisory only) |
| Tracking metric | hold-to-target SL ≤30%, TP1+ ≥50% |

**За:** максимальный edge на данных (n=37→135 short); совпадает с фактическим live (100% short);
упрощает архитектуру (закрывает long-пробел явно).  
**Против:** теряем потенциальный long bounce (gate 22% TP1); меньше сигналов (funnel 1.5%).

### H-B — «Широкий хантер»

**Определение:** текущая модель — short+long, 8 фаз, ignition/early_alert/prep_shadow параллельно.

| Параметр | Значение |
|----------|----------|
| Направление | short + long |
| Фазы | все lifecycle |
| Метрика | thesis_success + gate_edge per slice |

**За:** больше возможных сетапов; long raw backtest выглядит лучше short (28% vs 44% SL на
synthetic legs — но **без phase metadata**).  
**Против:** long confirmed слабый; live long=0; 6 detector paths; метрики путаны; sprawl 26k LOC
без фокуса. **Данные не поддерживают как primary product.**

### H-C — «Исслед-платформа»

**Определение:** минимум TG; максимум измерения — tick lake, feature_latch, gate_edge, walk-forward.
Production promotion только через edge-gate.

| Параметр | Значение |
|----------|----------|
| Live TG | выключен или digest-only |
| Output | edge-отчёты, dossier, calibration suggestions |
| Promotion | SL ≤ baseline на n≥30 |

**За:** честная наука; защита от переобучения на n=12 live.  
**Против:** заказчик хочет сигналы в TG; откладывает product value.

---

## 4. Решение G2 — H-B «Широкий хантер» (утверждено)

| Вопрос | Решение |
|--------|---------|
| Primary product | **H-B «Широкий хантер»** — short + long, все lifecycle-фазы |
| Long delivery | **TG disabled by default**; enable только после gate_edge long n≥30 и SL ≤35% |
| Multi-detector | Router: short_dump + long_bounce + early/ignition → prep_shadow/advisory |
| North Star (short) | confirmed hold-to-target SL ≤30%, TP1+ ≥50% (n≥30) |
| North Star (long) | confirmed hold-to-target SL ≤35%, TP1+ ≥25% (n≥30, отдельный гейт) |
| Per-phase KPI | отдельные таблицы gate_edge по `lifecycle_phase` × direction |
| Запретить как NS | `thesis_success`, live WR без hold-to-target пары |
| Lifecycle policy | per-direction exit policy (R4); `lifecycle_stale` не North Star |
| Принятый риск | long edge marginal (gate_edge TP1 22%); live long closes = 0 historically |

**G1:** полный rewrite → `hunt_core/` production-core; `hunt_watch` → `hunt/_legacy/` после cutover.

### 4.1 Interim scope (2026-06-13 ADR)

Пока live n<30 и только dump_active short доказан на gate_edge:

- **Live confirm TG:** short `dump_active` primary path
- **Advisory TG:** off by default (`HUNT_ADVISORY_TG=0`)
- **Long TG:** off (`HUNT_LONG_TG=0`, edge_policy)
- **Block:** confirm TG при `dump_active` + `bias=wait`
- **Measurement:** prep_shadow + gate_edge per slice before promote

См. [ADR-G2-REVIEW-2026-06-13.md](ADR-G2-REVIEW-2026-06-13.md) · [HUNT_TRUTH_AUDIT.md](HUNT_TRUTH_AUDIT.md)

---

## 5. Измеримые критерии успеха (H-B)

### 5.1 Research (offline, еженедельно)

| KPI | Target | Min n | Источник |
|-----|--------|-------|----------|
| Confirmed short SL | ≤30% | 30 | `gate_edge.py` |
| Confirmed short TP1+ | ≥50% | 30 | `gate_edge.py` |
| Confirmed long SL | ≤35% | 30 | `gate_edge.py` (гейт для TG) |
| Confirmed long TP1+ | ≥25% | 30 | `gate_edge.py` |
| dump_active short SL | ≤25% | 30 | gate_edge slice |
| Per-phase slice | SL ≤ aggregate+5pp | 30 | gate_edge matrix |
| Raw baseline SL | ~52% (контроль) | 30 | enriched backtest |
| New feature/gate | SL ≤ baseline | 30 | edge-gate rule |

### 5.2 Production (live, monthly)

| KPI | Target | Min n | Источник |
|-----|--------|-------|----------|
| Live short hold-to-target SL | ≤35% | 30 | join live→backtest grade |
| Live long hold-to-target SL | ≤40% | 30 | только если long TG enabled |
| tp_hit rate (short) | ≥25% | 30 | outcomes_report (honest) |
| Confirm funnel | ≥2% | — | signal_events |
| Early-exit net | ≥0 avoided/forfeited | 10 | `early_exit_verdict()` |

### 5.3 Anti-goals

- Не ослаблять `confirm_min` / fuel при backtest SL >30% (guardrail уже в коде).
- Не добавлять фичи в live confirm без gate_edge FAIL/PASS цикла (`dump_init_score` прецедент).
- Не использовать thesis_success как veto или North Star.

---

## 6. Scope matrix — что в продукт, что в archive

| Компонент | H-A Снайпер | H-B Широкий | H-C Research |
|-----------|-------------|-------------|--------------|
| `confirm_dump` + gate | **core** | core | measure |
| `confirm_long` live | **off** | core | measure |
| `dump_active` phase filter | **hard** | soft | tag only |
| ignition TG | off/log | optional | log |
| early_alert TG | off/log | optional | log |
| prep_shadow | calibration | calibration | primary |
| beat_* experiments | archive | archive | archive |
| `dump_init_score` | archive | archive | experiment |
| intel dossier | weekly report | weekly | primary output |

---

## 7. Воспроизведение анализа

```bash
# Gate edge (главный edge-доказатель)
.venv/bin/python3 hunt/scripts/gate_edge.py --direction both

# Live honest rollup
.venv/bin/python3 hunt/scripts/outcomes_report.py

# Prep-shadow slices
.venv/bin/python3 hunt/scripts/prep_shadow_report.py --json

# Calibration truth + early exit
.venv/bin/python3 -c "
from hunt_watch.bootstrap import bootstrap; bootstrap()
from hunt_watch.calibration import compute_backtest_rates, compute_gate_edge, early_exit_verdict
import json
print(json.dumps(compute_gate_edge(), indent=2))
print(json.dumps(early_exit_verdict(), indent=2))
"

# Phase×direction на gate_edge (ad-hoc)
.venv/bin/python3 -c "
import json; from collections import Counter, defaultdict
from pathlib import Path
rows=[json.loads(l) for l in Path('hunt/data/gate_edge_outcomes.jsonl').read_text().splitlines() if l.strip()]
# ... Counter by lifecycle_phase, direction, bt_outcome
"
```

---

## 8. Gate G2 — зафиксированные ответы

| # | Вопрос | Ответ |
|---|--------|-------|
| 1 | Целевой продукт | **H-B Широкий хантер** |
| 2 | Long | long с отдельным edge-гейтом (TG off until n≥30 PASS) |
| 3 | TG scope | confirmed short+long (когда гейт пройден) + ignition/early advisory |
| 4 | North Star | hold-to-target SL per direction/phase |
| 5 | lifecycle_stale | per-direction policy (R4); не North Star |

**Следующий шаг:** G3 — [HUNT_TARGET_ARCH.md](HUNT_TARGET_ARCH.md) + `hunt_core/contracts.py`.

---

*Связанные документы:* [HUNT_TRUTH_AUDIT.md](HUNT_TRUTH_AUDIT.md) ·
[HUNT_CHANGELOG.md](HUNT_CHANGELOG.md) · мастер-план v3 `.claude/plans/elegant-skipping-cloud.md`
