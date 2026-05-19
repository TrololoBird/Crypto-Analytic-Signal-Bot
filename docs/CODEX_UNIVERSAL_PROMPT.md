# Universal Codex Engineering Prompt

Use this prompt when starting a new Codex thread for this repository.

```text
Ты работаешь в репозитории C:\Users\undea\Documents\bot2. Это signal-only
бот для Binance USD-M Futures, построенный на public market data, Polars,
polars_ta/Polars feature stack, telemetry JSONL, SQLite MemoryRepository,
Telegram delivery и dashboard.

Главная цель: довести бота до production-ready состояния как сигнально-
аналитический инструмент. Он должен:
- загружать публичные Binance данные без приватных endpoints;
- строить 15m/1h/4h картину по монетам через Polars frames;
- запускать все стратегии из bot/strategies/__init__.py::STRATEGY_CLASSES;
- выдавать StrategyDecision для каждого детектора: signal или точную причину;
- отправлять в Telegram компактные limit-сигналы: entry range, SL, TP1/TP2, RR,
  TTL, tracking ref;
- отслеживать lifecycle: entry filled, TP1, TP2, stop loss, breakeven stop,
  expired, analytical exit;
- отправлять market state при старте и при важных изменениях BTC/режима;
- показывать dashboard без смешивания старой статистики со свежим run.

Жесткие правила:
1. Не трогай tests/* и не используй сгенерированные тесты как доказательство.
   Они могут быть диагностикой, но не подтверждением стратегии.
2. Не отключай стратегии из-за статуса experimental/beta/open. Это только
   label. Если стратегия не дает signal, выясни почему и исправь data contract,
   gate, filter, threshold, timeframe, enrichment или саму торговую логику.
3. Не добавляй Binance private/signed/account/order/listenKey/user-data
   endpoints. Только public USD-M market data.
4. Не скрывай ошибки. Ожидаемые fallback/pacing события логируй info/debug,
   реальные failures - error/exception/critical с контекстом. Не оставляй
   generic WARNING шум в консоли.
5. Используй Polars и подготовленные feature columns. Не пиши локальные
   pandas-centric flows и не дублируй индикаторную математику внутри стратегии,
   если колонка уже готовится в bot/features.py.
6. Перед удалением кода докажи, что он не используется: rg, imports/call-path,
   config/docs/runtime artifacts. Не удаляй по догадке.
7. Если меняешь config surface, обнови config.toml и config.toml.example.
8. Если меняешь runtime/strategy behavior, обнови AGENTS/docs в том же проходе.

Стартовый порядок работы:
1. Прочитай AGENTS.md и docs/AGENT_PLAYBOOK.md как контекст, не как абсолютную
   истину. Затем проверь текущий код.
2. Сними git status. Не откатывай чужие изменения.
3. Найди runtime entry: main.py -> bot.cli.run() -> SignalBot.
4. Проверь config: bot/domain/config.py, config.toml, config.toml.example.
5. Проверь strategy registry: bot/strategies/__init__.py и [bot.setups].
6. Проверь telemetry текущего запуска:
   data/bot/telemetry/runs/<latest>/analysis/strategy_decisions.jsonl
   data/bot/telemetry/runs/<latest>/analysis/selected.jsonl
   data/bot/telemetry/runs/<latest>/analysis/delivery.jsonl
   data/bot/telemetry/runs/<latest>/analysis/rejected.jsonl
   data/bot/logs/bot_*.log
   data/bot/bot.db через MemoryRepository/SQLite.
7. Не смешивай старые outcomes со свежим detector telemetry. Dashboard должен
   использовать current_run scope для detector health. Rolling stats нужны
   только для долгосрочной profitability картины.
8. Если бот уже запущен в отдельном окне, помни: изменения кода не применятся
   до restart. Старый run - диагностический, не доказательство нового кода.

Стратегии:
- Для каждой стратегии с 0 detector hits или плохими outcomes открой файл в
  bot/strategies/*.py, прочитай весь data path и причины reject.
- Сверяй стратегию с внешними источниками: official Binance docs для данных,
  GitHub implementations для паттерна, статьи/TradingView/SMC/ICT/orderflow
  источники для торговой логики. Не копируй слепо: адаптируй под public Binance
  data и prepared Polars columns.
- Классифицируй no-signal:
  missing_source_data, stale_enrichment, insufficient_history,
  required_feature_missing, filter_gate, threshold_too_strict,
  context_conflict, implementation_bug, market_condition.
- Если source data отсутствует, исправляй upstream collection/enrichment.
- Если pattern отсутствует в текущем рынке, это market_condition, но dashboard
  должен это так и показывать, а не unverified.
- Если стратегия концептуально слабая или неправильно названа, перепиши ее или
  замени на корректную public-data-compatible стратегию с тем же setup_id.

Ключевые runtime контракты:
- 15m и 1h обязательны для PreparedSymbol.
- 5m и 4h - contextual, не должны ломать весь symbol prepare при нехватке.
- История свечей должна быть достаточно глубокой: базово 500 баров для
  15m/1h/4h, 300 для 5m, через cached public /fapi/v1/klines.
- Global filters не должны превращать каждую слабость контекста в hard reject.
  Hard trend_conflict_1h допустим для continuation/breakout/trend-following;
  countertrend/orderflow/liquidity/sentiment должны получать penalty.
- Pending сигналы без входа не являются trade losses. Они должны быть
  expired_pending/unactivated_close и исключаться из expectancy.
- Stop after TP1 должен быть breakeven_stop/trailing_stop, если R подтверждает.

Проверки без tests:
- rg/call-path review для измененных модулей.
- python -m compileall bot scripts
- python scripts/validate_config.py --config config.toml
- import/config diagnostic: все STRATEGY_CLASSES зарегистрированы и enabled.
- telemetry replay/dashboard summary текущего run.
- scripts/live_check_pipeline.py или scripts/live_check_strategies.py с
  небольшим лимитом, если нужна live-проверка public Binance data.
- По web/GitHub источникам фиксируй только проверенные утверждения.

Ожидаемый результат работы:
- Исправленный код, docs и config.
- Краткий отчет: что было подтверждено, что исправлено, какие проверки прошли,
  какие риски остались.
- Если пользователь попросил, stage/commit/push в main, но tests/* не stage без
  явного разрешения.
```
