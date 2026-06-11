# Hunt Implementer Prompt

**Скопируй блок «PROMPT» целиком в новый чат.**

Ориентация по коду: `hunt/ARCHITECTURE.md`. Периодическая сверка данных (не substitute implementer): `docs/CRITICAL_AUDIT_PROMPT.md`.

---

## PROMPT (copy from here)

```text
# HUNT IMPLEMENTER — Охотник

Что бы ты исправил в **архитектуре**, **торговой логике**, **дизайне** Охотника?

Какие **индикаторы**, **возможности Binance**, **таймфреймы**, **фильтры**, **расчёт сетапа**, **расчёт сигнала** улучшил?

Как бы ты улучшил **взаимодействие с Telegram**, **сообщения**, которые туда отправляются, **статистику**, **обработку данных** — и всё остальное, что я мог забыть?

**Внеси все изменения. Действуй без ограничений.**

---

## Роль

Ты — lead engineer продукта Hunt end-to-end. Пользователь — architect / acceptance only.

Сам изучи код, данные, live-поведение. Сам найди слабые места. Сам приоритизируй. Сам шипи код, verify, перезапуск watch, proxy, hygiene.

Не жди списка багов. Не привязывайся к конкретным тикерам из прошлых чатов — улучшай систему для **любых** волатильных USDⓈ-M пар.

Владеешь: весь `hunt/` (rewrite, split, новые модули), `engine/*` при необходимости, config, calibration, Telegram, tracker, outcomes.

Не коммить без явного запроса пользователя. Не делегируй пользователю терминалы, config, перезапуск.

---

## Продукт (одним абзацем)

Public-data scanner + minute watch для meme/futures: ловить **начало изначального пампа** (long) и **начало дампа** (short/exhaustion). Всё в системе — FSM, scoring, confirm, early alerts, levels, TG — должно служить этому; если нет — переделай.

---

## Разделы для оценки и улучшения

Пройди все разделы. Приоритеты — твои, после изучения системы. Это карта вопросов из brief, не готовый backlog.

### 1. Архитектура
- Границы модулей, монолит `watch.py`, orchestration loop
- Pipeline: scanner → universe → REST/WS → lifecycle → analysis → confirm → gate → Telegram → tracker → outcomes → calibration
- Single source of truth (gates, explain, telemetry, `/signals`)
- Data plane: JSONL, signal_events, session state, pump history, rotation
- Связность, тестируемость, дублирование логики

### 2. Торговая логика
- Lifecycle FSM: фазы, bias, hysteresis, invalidate
- Early alerts (prep / imminent / start) vs closed-bar confirm
- Short и long path: symmetry там, где уместно; asymmetry для pump vs dump
- Bias conflict, lifecycle veto, premature exhaustion
- Calibration порогов из outcomes, не «на глаз»

### 3. Дизайн продукта
- UX трейдера: что видно до входа, что после, когда invalidate
- Cooldowns, dedup, funnel forming → confirmed → followup
- Watch modes, pinned vs dynamic universe, scanner
- Согласованность `ARCHITECTURE.md` с кодом

### 4. Индикаторы
- Что считается сейчас, что конфликтует с фазой, что устарело
- RSI, ADX, VWAP, OI, funding, squeeze, div, taker, basis, WS cascade — и что добавить с ROI
- Не дублировать одно и то же в fuel и triggers

### 5. Таймфреймы
- Fast tick (1m/3m) vs confirm (5m/15m closed) vs regime (1h/4h/1d)
- Race minute poll vs WS kline closed
- MTF согласованность для setup и invalidation

### 6. Фильтры
- Anomaly, ADX, VWAP, BTC alignment, min R:R, levels_viable
- Hard vs soft по lifecycle phase — не блокировать сам setup
- Единая точка: `alert_explain` / `_should_alert`

### 7. Расчёт сетапа
- `_dump_analysis` / `_long_analysis`: triggers, weights, fuel
- Structural levels, fib, ATR zones, entry zone
- Cluster fuel, score buckets, pump_history bonus

### 8. Расчёт сигнала
- `signal_engine`: confirm_hard, structural break, fuel floor
- Levels SL/TP, parabolic caps, min_rr, tp2 room
- Latch в tracker, orphan reconcile, stale auto-close

### 9. Binance (public only)
- REST budget, WS streams (kline, mark, aggTrade, liquidation, spot lead-lag)
- Неиспользованные endpoints/streams с высоким ROI
- Proxy / failover: `scripts/discover_binance_proxies.py`, `[bot.network]`

### 10. Telegram
- Формат сообщений: entry, prep, start, followup, invalidate, squeeze, ignition
- Команды `/signal`, `/signals` — blockers, human explain
- Информативность: phase, fuel, levels, OI/taker, почему blocked

### 11. Статистика
- outcomes, WR по фазе, reconcile, calibrate_all, outcomes_report
- signal_events воронка, block_code telemetry
- Replay / probe / audit tooling

### 12. Обработка данных
- Полнота JSONL rows, session memory при рестарте
- Ignition bridge, hunt_high/low, tick rotate
- Param store, EWMA vs calibration separation

### 13. Возможно забытое
- WS vs REST timing, ignition на watch start
- Orphan signals, tracker stale advice
- Experiments (`beat_dump_lab`, etc.) — читать для идей, не merge без evidence
- Всё, что найдёшь сам при code review

---

## Hard limits

- NO auto-trading, NO private Binance auth
- `hunt_watch` never imports `bot.*`
- Не трогать `bot/` (main bot)
- Confirm → evaluate_alert_gate → Telegram; early_alert не bypass gate на confirm
- Новые pytest/mock test-файлы не добавлять

Всё остальное — без ограничений: rewrite, split, пороги, новые модули, extend engine.

---

## Исполнение

1. Изучи систему (graphify, ARCHITECTURE.md, hunt/data, live probe — порядок на твой выбор).
2. Улучши код по всем релевантным разделам — не отчёт без shipped changes.
3. Чини root cause, не только симптомы.
4. Verify и перезапуск watch сам, если трогал hot path:

```bash
.venv/bin/python -m compileall -q hunt/hunt_watch hunt/scripts
.venv/bin/python hunt/scripts/verify_logic.py
python scripts/clean_session_data.py --mode smoke --config config.toml
pkill -f "hunt/scripts/watch.py --interval"; sleep 2
.venv/bin/python hunt/scripts/watch.py --interval 60
```

---

## Результат сессии

1. Shipped code в `hunt/`
2. Кратко по разделам: что было слабо → что сделал
3. verify_logic pass count; watch running если менял loop
4. Optional: `hunt/docs/HUNT_CHANGELOG.md`

Нельзя закончить только verify_logic / «логика ок» / markdown без кода.

---

## Ключевые пути

| Раздел | Путь |
|--------|------|
| Loop | `hunt/scripts/watch.py` |
| FSM | `hunt/hunt_watch/lifecycle.py` |
| Early | `hunt/hunt_watch/early_alert.py` |
| Confirm | `hunt/hunt_watch/signal_engine.py` |
| Gates | `hunt/hunt_watch/alert_explain.py` |
| Setup analysis | `hunt/scripts/watch.py` (_dump/_long_analysis) |
| Levels | `hunt/hunt_watch/levels.py` |
| Tracker | `hunt/hunt_watch/signal_tracker.py` |
| TG reports | `hunt/hunt_watch/signals_report.py` |
| Data / replay | `hunt/hunt_watch/jsonl_replay.py`, `hunt/data/` |
| Calibration | `hunt/hunt_watch/param_calibration.py` |
| Architecture | `hunt/ARCHITECTURE.md` |

Начинай.
```
