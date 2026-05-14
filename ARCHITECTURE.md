# Архитектура проекта

## Поток данных

```text
Binance public USD-M REST/WS -> market_data/ws_manager
                              -> application/EventBus
                              -> SymbolAnalyzer/CycleRunner/KlineHandler
                              -> features.py + features_* modules
                              -> SignalEngine + strategies/*.py
                              -> filters/scoring/confluence/ML guardrails
                              -> tracking + delivery + telemetry/dashboard
```

## Ключевые контракты
- `BaseSetup.detect(prepared, settings) -> Signal | None | StrategyDecision`
- `PreparedSymbol` (`bot/domain/schemas.py`) содержит `work_15m/work_1h/work_4h/work_5m` и market context
- `Signal` — основной контракт торгового сигнала
- Public runtime feature payload is pinned by `bot/domain/contracts.py`
- Runtime Binance boundary is public USD-M market data only: no signed REST, account/trade endpoints, `listenKey`, private WS, or user-data streams

## Где менять что
- Индикаторы: `bot/features.py`, `bot/features_core.py`, `bot/features_advanced.py`, `bot/features_microstructure.py`, `bot/features_oscillators.py`, `bot/features_structure.py`
- Стратегии: `bot/strategies/`, exported via `bot/strategies/__init__.py`
- Runtime orchestration: `bot/application/` (`SignalBot`, `ShortlistService`, `CycleRunner`, `HealthManager`, `TelemetryManager`, `FallbackRunner`, `OIRefreshRunner`)
- Фильтрация: `bot/filters.py`, `bot/scoring.py`, `bot/confluence.py`, `bot/ml/`
- Доставка: `bot/delivery.py`, `bot/messaging.py`, `bot/telegram/`
- Трекинг/исходы: `bot/tracking.py`, `bot/outcomes.py`, `bot/core/memory/repository.py`
- Dashboard/metrics: `bot/dashboard.py`, `bot/metrics.py`
