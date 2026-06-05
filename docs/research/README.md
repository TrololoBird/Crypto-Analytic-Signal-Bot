# Research: Binance USD-M Telegram Signal Bot (target spec)

**Целевая спецификация** для большого изменения проекта (веб/OSS/Binance + инженерный синтез).

**Текущий код `bot2` — не истина:** AI-generated, активный рефакторинг. Снимок только для миграции: [LEGACY_CODE_SNAPSHOT.md](LEGACY_CODE_SNAPSHOT.md).

**Ревью spec:** [PLAN_CRITICAL_REVIEW.md](PLAN_CRITICAL_REVIEW.md).

**Агентам:** не читать весь каталог — [../AGENT_TOKEN_POLICY.md](../AGENT_TOKEN_POLICY.md). Статус v1: [../DEFINITION_OF_DONE.md](../DEFINITION_OF_DONE.md). LLM APIs: [LLM_API_INTEGRATION.md](LLM_API_INTEGRATION.md).

## Documents

| File | Contents |
|------|----------|
| **[PLAN_CRITICAL_REVIEW.md](PLAN_CRITICAL_REVIEW.md)** | Ревью согласованности **target spec** (не «код = правда») |
| **[LEGACY_CODE_SNAPSHOT.md](LEGACY_CODE_SNAPSHOT.md)** | Опциональный снимок legacy — только для миграции |
| **[PROJECT_ARCHITECTURE.md](PROJECT_ARCHITECTURE.md)** | **Full spec: modules, data flow, indicators, TFs, candle/SMC, storage, dashboard, autotune** |
| [SIGNAL_ONLY_PRODUCT.md](SIGNAL_ONLY_PRODUCT.md) | **No auto-trading** — manual TG only; implications for setups/filters/TF (web) |
| [SIGNAL_EVALUATION.md](SIGNAL_EVALUATION.md) | **Signal scoring pipeline** — detect → score → confluence → gate → tier → TG |
| [BENCHMARK_ANCHORS.md](BENCHMARK_ANCHORS.md) | **BTC ETH SOL XRP XAU XAG PAXG** — always-on max data + stricter ACTION |
| [STRATEGY_MANUAL_SUITABILITY.md](STRATEGY_MANUAL_SUITABILITY.md) | **M1/M2/W/C/R** — какие из 38 стратегий для ручного канала (не пороги) |
| [CRYPTO_SIGNAL_COMPARISON.md](CRYPTO_SIGNAL_COMPARISON.md) | vs [Crypto-Signal](https://github.com/CryptoSignal/Crypto-Signal) — почему не «тысячи алертов» |
| [CONNECTOR_DECISION.md](CONNECTOR_DECISION.md) | Thin aiohttp vs CCXT Pro vs official SDK |
| [SIGNAL_COLLISION_AND_DEDUP.md](SIGNAL_COLLISION_AND_DEDUP.md) | 5–38 hits на одну монету: что в TG, merge, gaps |
| [ORDER_FLOW_INGEST.md](ORDER_FLOW_INGEST.md) | aggTrade, depth, `!forceOrder@arr`, OSS comparison |
| [SIGNAL_BOT_LANDSCAPE.md](SIGNAL_BOT_LANDSCAPE.md) | OSS categories, anti-patterns, reference projects |
| [LLM_API_INTEGRATION.md](LLM_API_INTEGRATION.md) | Claude/Gemini/Cursor — где в боте (не hot path) |
| [TARGET_ARCHITECTURE.md](TARGET_ARCHITECTURE.md) | Short layer diagram + pipeline A–H (details in PROJECT_ARCHITECTURE) |
| [BINANCE_PUBLIC_DATA_MATRIX.md](BINANCE_PUBLIC_DATA_MATRIX.md) | REST/WS public endpoints, weights, scheduling |
| [TELEGRAM_CHANNEL_SPEC.md](TELEGRAM_CHANNEL_SPEC.md) | Tiered channel, message templates, trust, cadence |
| [STRATEGY_CATALOG.md](STRATEGY_CATALOG.md) | 38 detectors: web-verified cards, thresholds, tiers |
| [GAP_ANALYSIS_BOT2.md](GAP_ANALYSIS_BOT2.md) | **Work queue** — реализация spec (не audit «код прав») |
| [WEB_RESEARCH_SUPPLEMENT.md](WEB_RESEARCH_SUPPLEMENT.md) | **Веб-усиление** — Binance/TG/SMC/screener/trust (May 2026) |
| [IMPLEMENTATION_READY.md](IMPLEMENTATION_READY.md) | **Sign-off gate** — готовность к execute |
| **[TARGET_REPOSITORY_LAYOUT.md](TARGET_REPOSITORY_LAYOUT.md)** | **Удалить / изменить / финальные файлы + deps + PR order** |

## Source plan

Consolidated from Cursor plan `архитектура_signal-bot_755ea003.plan.md` (research iteration, May 2026).

## Reading order

1. [SIGNAL_ONLY_PRODUCT.md](SIGNAL_ONLY_PRODUCT.md) — границы продукта (не автоторговля)  
2. [PROJECT_ARCHITECTURE.md](PROJECT_ARCHITECTURE.md) — модули и данные  
3. [SIGNAL_EVALUATION.md](SIGNAL_EVALUATION.md) — как оценивается сигнал  
4. [BENCHMARK_ANCHORS.md](BENCHMARK_ANCHORS.md) — якорные монеты  
5. [STRATEGY_CATALOG.md](STRATEGY_CATALOG.md) — 38 детекторов  
6. [TELEGRAM_CHANNEL_SPEC.md](TELEGRAM_CHANNEL_SPEC.md) — канал  
7. [GAP_ANALYSIS_BOT2.md](GAP_ANALYSIS_BOT2.md) — очередь работ P0–P3  

Strategy concepts: web research — sources in `STRATEGY_CATALOG.md`. Binance API: official public docs only.
