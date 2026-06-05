# LLM API integration (Cursor / Claude / Google) — где уместно

> **Инвариант:** hot path (WS → Polars → strategies → delivery) остаётся **детерминированным**. LLM не выбирает сделки и не ставит ордера.

## Сводка

| API | В runtime боте? | Где уместно |
|-----|-----------------|-------------|
| **Anthropic (Claude)** | Опционально, **off** по умолчанию | Operator summaries, rationales в intelligence |
| **OpenAI / Google Gemini** | То же | Альтернативный провайдер для тех же слоёв |
| **Cursor API** | **Нет** | Только IDE/CI агенты — не в `main.py run` |

---

## Слой 1 — Hot path (запрещено)

```
WS kline → prepare_frame → SignalEngine → contract → confluence → deliver
```

- Никаких HTTP к LLM в этом цикле (latency, cost, nondeterminism).
- Сигналы только из rule-based / Polars стратегий.

---

## Слой 2 — Intelligence / operator (рекомендуется, optional)

Уже есть:

- `bot/market/enrichment.py` — `PublicIntelligenceService`
- `IntelligenceConfig` в `bot/domain/config.py`
- `market_context.intelligence_json`, `telegram_html` в SQLite

**Куда встроить Claude/Gemini:**

| Функция | Триггер | Выход |
|---------|---------|-------|
| Session brief | Каждые N мин / startup report | `telegram_html` для оператора (не канал) |
| Signal rationale | После ACTION candidate (pre-send) | Доп. поле в сообщении «почему» (human review) |
| Regime narrative | `market_regime` change | Строка в dashboard operator tab |

**Реализация (v1.1):**

```text
bot/intelligence/llm_provider.py   # protocol: complete(prompt) -> str
bot/intelligence/anthropic.py      # ANTHROPIC_API_KEY
bot/intelligence/google.py         # GOOGLE_API_KEY / Gemini
scripts/llm_smoke.py               # offline test, no live loop
```

Config (пример, не включено):

```toml
[bot.intelligence.llm]
enabled = false
provider = "none"  # anthropic | openai | google
max_tokens_per_hour = 50000
use_on_hot_path = false  # must stay false
```

---

## Слой 3 — Offline / research (рекомендуется)

| Задача | Инструмент |
|--------|------------|
| Harvest JSONL analysis | Cursor/Claude **в IDE**, не в боте |
| Strategy redesign | Agent + `data/research_harvest/` |
| Calibration review | `calibration_pipeline` + agent summary |
| Zero-hit triage | Skill `zero-hit-strategy-triage` |

`python main.py harvest` **без** LLM — только сбор данных.

---

## Cursor API

- **Назначение:** автоматизация разработки (агенты, CI, code review).
- **Не** подключать к `SignalBot` — другой продукт, другие ключи, нет real-time market context.
- Используй: Cursor IDE, Cloud Agents, `scripts/agent_bot_supervisor.py` для **ops**, не signal generation.

---

## Google API (Gemini)

- Уместен как **второй провайдер** в `bot/intelligence/` для тех же задач, что Claude.
- **Не** для котировок — Binance public API уже в `bot/market/`.
- Плюс: дешёвые long-context для разбора больших harvest JSONL **offline**.

---

## Безопасность

- Ключи только в `.env` (`ANTHROPIC_API_KEY`, `GOOGLE_API_KEY`) — hooks блокируют commit/read.
- LLM **не** получает Binance API keys (их нет в проекте).
- LLM output **не** bypass confluence — максимум текст в Telegram после gates.

---

## Решение для v1

**Не внедрять** LLM в runtime до стабильного harvest + ops loop.  
Документ и config stub — достаточно; код провайдера — backlog `OPT-2` в [DEFINITION_OF_DONE.md](../DEFINITION_OF_DONE.md).
