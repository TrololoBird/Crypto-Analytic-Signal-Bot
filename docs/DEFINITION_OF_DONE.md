# Definition of Done (v9 signal-only bot)

> **Один источник правды:** что значит «проект готов», vs что — **непрерывная эксплуатация** (не новые фичи).

## Почему каждый раз «50 улучшений»?

| Причина | Объяснение |
|---------|------------|
| Запрос без DoD | «Найди улучшения» = сравнение с **всем OSS** — список почти бесконечен |
| Нет freeze v1 | Пока нет зафиксированного v1, любой аудит находит **следующий** слой |
| Разные цели | Research backlog ≠ баги ≠ ops — их смешивают в один список |

**Правило для агентов:** не генерировать новый список из 50 пунктов. Только ID из таблицы backlog ниже.

---

## Freeze: не плодить файлы

- **208 `.py` в `bot/`** (было ~222). Новый файл только если заменяет **≥2** старых.
- **Запрещено** split без запроса: `ws.py`, `rest_impl.py`, `symbol_analyzer.py`, `session_ops.py`, `runtime_ops.py`, `memory.py`, `tracking.py`.
- **Слито (2026-06-04):** `ws_*`→`ws.py` · `rest_*`→`rest_impl.py` · `runtime/*`→`runtime_ops.py` · `analyzer/*`→`analyzer_ops.py` · telemetry+live_watch+runtime_analysis→`session_ops.py` · ex-`analyzer/`→`symbol_analyzer.py` · outcomes SQL→`memory.py`.
- **Не трогаем:** 45× `bot/strategies/` (каталог).
- Допустимо: правки внутри существующих файлов, ops, багфиксы.

---

## Как выглядит «завершённый» продукт (v1)

**Не** HFT-бот и **не** auto-trader. **Да:**

1. **Signal factory** — public Binance USD-M → trade plans → Telegram.
2. **42 стратегии** в `bot/strategies/`.
3. **Delivery invariant** — `validate_signal_contract` → `hard_confluence_gate` (3/5) → `deliver`.
4. **Shortlist** ~40–55 + anchors, tier WATCH/ACTION, R-class WATCH-only.
5. **Ops loop** — `run` / `harvest` / supervised 6h / rollup / calibration (gated).
6. **Нет** private Binance API, **нет** регистрации в боте.

---

## v1 code — статус (2026-06-04)

| Критерий | Статус |
|----------|--------|
| v9 packages + refactor gate | ✅ |
| Waves E1–F11 + W0–W4 | ✅ |
| Research harvest | ✅ |
| Calibration gated | ✅ |
| F11: stdout `strategy_decision` + live_watch fallback | ✅ |
| Analyzer pipeline re-merged (no further splits) | ✅ |
| Live 6h + proxy | ✅ (сессии 2026-06-04) |
| `use_weighted_confluence` tuned | ✅ OPS-2 (2026-06-04, live_check review) |

**Вывод:** v1 **продуктово готов**; дальше только ops из таблицы.

---

## Открытый backlog (единственный список работ)

| ID | Задача | Блокер v1? |
|----|--------|------------|
| OPS-1 | Harvest 2h+ → strategy redesign notes | Нет (`scripts/run_research_harvest.sh`) |
| OPS-2 | Enable `use_weighted_confluence` after telemetry review | ✅ 2026-06-04 |
| OPS-3 | Nightly `make nightly-calibration` when REST OK | Нет (`scripts/run_nightly_calibration.sh`) |
| OPT-1 | Prometheus → Grafana dashboard | Нет |
| OPT-2 | Optional LLM rationales (intelligence layer) | Нет |

**Отменено (не возвращать без запроса):**

| ID | Причина |
|----|---------|
| V1.1-1 | Split `memory.py` / `tracking.py` — больше файлов, хуже для агентов |
| V1.1-2 | Split `ws.py` — то же |
| Generic «50 improvements» audit | Бесконечный цикл планирования |

**Закрыто:** duplicate detectors, delivery bypass, W0–W4, F11 stdout bridge.

---

## Verification (v1 ship check)

```bash
source .venv/bin/activate
make check
python scripts/validate_config.py --config config.toml
PYTEST_LIVE=1 pytest tests/live/ -v
```

---

## Для агентов

v1 = signal-only factory в проде. Работать по **OPS/OPT** из таблицы. **Не** дробить файлы. **Не** новый список из 50 пунктов. Контекст: [AGENT_TOKEN_POLICY.md](AGENT_TOKEN_POLICY.md).
