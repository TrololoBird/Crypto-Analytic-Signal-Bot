# Критическое и техническое ревью research-плана

Дата: 2026-05-31. Ревью **целевой спецификации** и внутренней согласованности `docs/research/*` + плана большого изменения.

## 0. Методология (обязательно)

| Источник | Роль |
|----------|------|
| **Веб/OSS/Binance docs** | Первичная правда для продукта, данных, доверия канала, anti-patterns |
| **`docs/research/*` + план** | Целевая архитектура **после** большого рефакторинга |
| **Текущий код `bot2`** | **Не истина.** AI-generated, активный рефакторинг, может быть неверным или устаревшим. Только **опциональный** снимок «что есть сейчас» — см. [LEGACY_CODE_SNAPSHOT.md](LEGACY_CODE_SNAPSHOT.md) |

**Ошибка предыдущего прохода ревью:** трактовка кода как эталона и правка spec «под фактический pipeline». Это **отменено**. Спор «spec vs код» решается в пользу **утверждённой target spec**, если нет противоречия с Binance API / signal-only.

---

## 1. Вердикт по target spec

| Оценка | Комментарий |
|--------|-------------|
| **Продукт** | Signal-only, manual TG, trade plan — согласовано с веб-практиками ([SIGNAL_ONLY_PRODUCT.md](SIGNAL_ONLY_PRODUCT.md)) |
| **Данные** | Public-only matrix, anchors, order flow — согласовано с Binance public API |
| **Анти-спам** | Shortlist + 8–15 families + WATCH/ACTION + MetaSignal — против Crypto-Signal volume model |
| **38 стратегий** | Каталог + M1/M2/W/C/R — внутренне согласован (21+9+4+4) |
| **Коннектор** | Thin aiohttp hot path — согласовано с guardrails |

---

## 2. Исправления внутри spec (не «код прав»)

### 2.1 Единый целевой pipeline (нормативный)

Документ [SIGNAL_EVALUATION.md](SIGNAL_EVALUATION.md) описывает **целевой** порядок:

1. Детекция по **lanes** (8–15 families / symbol / `trigger_tf`)
2. **TradePlanBuilder** — zone, SL, TP, invalidation
3. **Scoring + ConfluenceEngine** (взвешенная модель)
4. **Contract** + **hard confluence gate** (3-of-5)
5. **MetaSignalMerger** — 1 ACTION / symbol / window
6. **Tier** WATCH vs ACTION + caps
7. Telegram + journal + tracking (без исполнения)

Старый черновик «отдельный score_signal до confluence» как **обязательный шаг** — убран; факторы scoring входят в confluence blend (как в PROJECT_ARCHITECTURE §11).

### 2.2 Коллизии

Target: [SIGNAL_COLLISION_AND_DEDUP.md](SIGNAL_COLLISION_AND_DEDUP.md) §3 — MetaSignal, 1 direction, family clusters, cross-window conflict blocker. **Не** привязывать дизайн к текущему `select_and_rank` в legacy коде.

### 2.3 Стратегии на символ

**Target:** 8–15 families на shortlist symbol, не 38× на каждый tick ([STRATEGY_MANUAL_SUITABILITY.md](STRATEGY_MANUAL_SUITABILITY.md) §5). Scheduler по `trigger_tf` — обязательный элемент большого изменения (P0 в плане).

### 2.4 R-class

**Target:** depth/spread/walls/velocity — WATCH-only или 15m aggregate redesign; **не** solo ACTION ([STRATEGY_MANUAL_SUITABILITY.md](STRATEGY_MANUAL_SUITABILITY.md)).

### 2.5 Якоря

**Target:** BTC, ETH, SOL, **XRP**, XAU, XAG, PAXG — always-on max data + stricter ACTION ([BENCHMARK_ANCHORS.md](BENCHMARK_ANCHORS.md)). В legacy снимке XRP мог отсутствовать — **не аргумент против spec**.

### 2.6 Именование gate `microstructure`

В **target** hard gate: явно разделить **book/CVD micro** (confluence component) и **positioning** (funding/OI) — не смешивать в одном boolean key.

---

## 3. Согласованность документов (checklist)

| Пара вопросов | Статус |
|---------------|--------|
| PROJECT_ARCHITECTURE ↔ TARGET_ARCHITECTURE | OK |
| TELEGRAM caps ↔ SIGNAL_ONLY | OK |
| CRYPTO_SIGNAL hybrid ↔ shortlist | OK |
| CONNECTOR ↔ BINANCE_PUBLIC_DATA_MATRIX | OK |
| STRATEGY_CATALOG 38 ↔ STRATEGY_MANUAL M1/M2 | OK (21+9) |
| ORDER_FLOW ↔ PROJECT data plane | OK |

---

## 4. Допущения (явные, до калибровки live)

| ID | Допущение |
|----|-----------|
| B1 | Числовые пороги в STRATEGY_CATALOG — стартовые из литературы, калибруются после deploy |
| B2 | 15–40 ACTION/day — продуктовый target, не гарантия рынка |
| B3 | Liquidation WS — proxy (Binance rate limit per symbol) |
| B4 | Autotune — offline only, не меняет hot-path gates без approve |

---

## 5. Что входит в «большое изменение» (план, не legacy)

Приоритет **реализации target spec** (после sign-off):

1. **P0** — Multi-TF scheduler + WS interval union + strategy lanes (8–15)
2. **P0** — Universe screener → shortlist 40–55 + 7 anchors
3. **P1** — MetaSignalMerger + tiered WATCH/ACTION + TELEGRAM_CHANNEL_SPEC
4. **P1** — TradePlanBuilder единый
5. **P2** — R-class policy + anchor floors
6. **P3** — Dashboard funnel + public audit ledger

Legacy код может быть **переписан**, а не «подогнан» — см. [docs/REFACTOR_PLAN.md](../REFACTOR_PLAN.md).

---

## 6. Веб-усиление (2026-05-31)

Усилено: liquidation WS (largest/1000ms), WS 1024/10msg/s, TradFi public vs agreement, TG flood limits, ACTION cadence, screener thresholds, FVG ICT, audit SHA256.

[WEB_RESEARCH_SUPPLEMENT.md](WEB_RESEARCH_SUPPLEMENT.md) · [IMPLEMENTATION_READY.md](IMPLEMENTATION_READY.md)

## 7. OSS landscape

[SIGNAL_BOT_LANDSCAPE.md](SIGNAL_BOT_LANDSCAPE.md), [CRYPTO_SIGNAL_COMPARISON.md](CRYPTO_SIGNAL_COMPARISON.md), [CONNECTOR_DECISION.md](CONNECTOR_DECISION.md)

---

## 7. Маркировка в документах (новая)

- **`[spec]`** — нормативная целевая архитектура (default)
- **`[legacy]`** — опционально: как может выглядеть текущий снимок кода; **не ограничивает** design

Убрана нормативная метка **`[bot2 today]`** из основных разделов.
