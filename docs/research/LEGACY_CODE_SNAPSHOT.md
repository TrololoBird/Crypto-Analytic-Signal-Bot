# Legacy code snapshot (не истина)

**Назначение:** опциональная справка для миграции во время рефакторинга. Проект в разработке, код сгенерирован/переписывается ИИ — **не использовать как эталон продукта**.

**Источник истины:** [PROJECT_ARCHITECTURE.md](PROJECT_ARCHITECTURE.md), [TARGET_ARCHITECTURE.md](TARGET_ARCHITECTURE.md), план `архитектура_signal-bot_755ea003.plan.md`.

## Зачем этот файл

При планировании большого изменения иногда полезно знать, **где сейчас лежит логика** — чтобы не дублировать модули вслепую. Любое расхождение с `[spec]` решается **в пользу spec**.

## Примерные точки входа (могут меняться)

| Область | Где сейчас (снимок) | Куда целим (v9 spec) |
|---------|---------------------|----------------------|
| Runtime loop | `bot/runtime/`, legacy `bot/application/` | `bot/runtime/` slim |
| Market data | `bot/market/`, `bot/ws_manager.py` | `bot/market/` |
| Strategies | `bot/strategies/` | без изменения пакета |
| Delivery | `bot/delivery.py`, `delivery_orchestrator` | `bot/delivery/` Phase 2 |
| Engine | `bot/engine/` | registry + lanes |

## Известные расхождения spec ↔ снимок (не блокеры spec)

| Topic | Снимок может иметь | Target spec |
|-------|-------------------|-------------|
| Kline trigger | 15m-only handler | Per-`trigger_tf` scheduler |
| Strategies/symbol | Все enabled | 8–15 lanes |
| TG tiers | Смешанные пути | Unified WATCH/ACTION orchestrator |
| Anchors | 6 symbols без XRP | 7 anchors incl. XRP |

Детальный diff **не** ведётся построчно — при рефакторинге переписываем под [GAP → заменён этим файл + план P0–P3].

См. [PLAN_CRITICAL_REVIEW.md](PLAN_CRITICAL_REVIEW.md) §0.
