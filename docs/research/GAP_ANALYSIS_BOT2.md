# Migration notes: target spec → codebase

> **Переименование смысла:** это не «gap = код прав». Это **очередь работ** для большого изменения: target spec ([PROJECT_ARCHITECTURE.md](PROJECT_ARCHITECTURE.md)) vs **временный** legacy snapshot. См. [LEGACY_CODE_SNAPSHOT.md](LEGACY_CODE_SNAPSHOT.md).

Snapshot: May 2026.

## Принцип

| | |
|---|---|
| **Правда** | `docs/research/*` + веб/OSS/Binance |
| **Legacy** | Текущий `bot2` — реализует spec **частично и нестабильно** |

## Implementation status (2026-06-01)

| P0 item | Status |
|---------|--------|
| Multi-TF scheduler + WS union | Wired (`kline_handler`, `market/scheduler`) |
| **8–15 strategy lanes** | Wired: `select_lane_setups` → `SignalEngine._route_strategies` when `runtime.enable_strategy_lanes=true` and `event_interval` set from `cycle_runner` |
| Shortlist 40–55 | Wired (`market/universe`, screener) |

`route_all_enabled_strategies=true` bypasses lanes (debug / legacy override).

## Work queue (приоритет реализации spec)

| P | Target capability | Заметка для рефакторинга |
|---|-------------------|-------------------------|
| **P0** | `StrategyTimeframeProfile` + scheduler on each `KlineClose` | Done — multi-interval kline path |
| **P0** | 8–15 strategy lanes per symbol | Done — `enable_strategy_lanes` + `event_interval` on hot path |
| **P0** | Universe screener → shortlist 40–55 | Done |
| **P1** | MetaSignalMerger + direction conflict 4h | Partial — merge + WATCH conflict delivery; ledger feeds 4h window |
| **P1** | Tiered WATCH/ACTION + burst/daily caps | Partial — per-cycle caps wired |
| **P1** | Unified TradePlanBuilder | Partial |
| **P2** | 7 anchors incl. **XRPUSDT** + stricter ACTION | Partial — `anchor_action_score_delta` in `classify_tier` |
| **P2** | R-class WATCH-only / redesign | Done — `r_class_watch_only` |
| **P3** | Operator funnel dashboard | Partial — REST funnel + WS `funnel_update` / `ws_health` after each cycle |
| **P3** | Public audit CSV + SHA256 | Partial — ledger + `/api/v1/public-audit` |

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
