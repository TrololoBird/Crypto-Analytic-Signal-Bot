# Migration notes: target spec → codebase

> **Переименование смысла:** это не «gap = код прав». Это **очередь работ** для большого изменения: target spec ([PROJECT_ARCHITECTURE.md](PROJECT_ARCHITECTURE.md)) vs **временный** legacy snapshot. См. [LEGACY_CODE_SNAPSHOT.md](LEGACY_CODE_SNAPSHOT.md).

Snapshot: May 2026.

## Принцип

| | |
|---|---|
| **Правда** | `docs/research/*` + веб/OSS/Binance |
| **Legacy** | Текущий `bot2` — реализует spec **частично и нестабильно** |

## Work queue (приоритет реализации spec)

| P | Target capability | Заметка для рефакторинга |
|---|-------------------|-------------------------|
| **P0** | `StrategyTimeframeProfile` + scheduler on each `KlineClose` | Заменить 15m-only kline path |
| **P0** | 8–15 strategy lanes per symbol | Не 38× detect на каждый tick |
| **P0** | Universe screener → shortlist 40–55 | Light refresh + deep refresh |
| **P1** | MetaSignalMerger + direction conflict 4h | [SIGNAL_COLLISION_AND_DEDUP.md](SIGNAL_COLLISION_AND_DEDUP.md) |
| **P1** | Tiered WATCH/ACTION + burst/daily caps | [TELEGRAM_CHANNEL_SPEC.md](TELEGRAM_CHANNEL_SPEC.md) |
| **P1** | Unified TradePlanBuilder | Zones, TTL, invalidation |
| **P2** | 7 anchors incl. **XRPUSDT** + stricter ACTION | [BENCHMARK_ANCHORS.md](BENCHMARK_ANCHORS.md) |
| **P2** | R-class WATCH-only / redesign | [STRATEGY_MANUAL_SUITABILITY.md](STRATEGY_MANUAL_SUITABILITY.md) |
| **P3** | Operator funnel dashboard | Funnel, zero-hit, WS health |
| **P3** | Public audit CSV + SHA256 | Trust ledger |

## Spec areas (всё в research pack)

| Area | Document |
|------|----------|
| Product boundary | [SIGNAL_ONLY_PRODUCT.md](SIGNAL_ONLY_PRODUCT.md) |
| Modules + data | [PROJECT_ARCHITECTURE.md](PROJECT_ARCHITECTURE.md) |
| Evaluation pipeline | [SIGNAL_EVALUATION.md](SIGNAL_EVALUATION.md) |
| 38 strategies | [STRATEGY_CATALOG.md](STRATEGY_CATALOG.md) |
| Channel | [TELEGRAM_CHANNEL_SPEC.md](TELEGRAM_CHANNEL_SPEC.md) |
| Connector | [CONNECTOR_DECISION.md](CONNECTOR_DECISION.md) |

## Recommended implementation order

1. Multi-TF scheduler + WS interval union  
2. Lanes + screener shortlist  
3. MetaSignalMerger + tiered delivery  
4. TradePlanBuilder  
5. Anchor policy + R-class  
6. Dashboard + audit  

См. [docs/REFACTOR_PLAN.md](../REFACTOR_PLAN.md), план §15–18.
