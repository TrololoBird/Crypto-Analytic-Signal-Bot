# Пересмотренный глубокий аудит `crypto-signal-analytic` (Signal-Only Bot)

## 1. Пересмотренная архитектурная оценка с учётом Signal-Only природы

### 1.1. Корректность взаимодействия с Binance Public API (без приватных ключей)

Проект **корректно реализует взаимодействие с Binance исключительно через публичные API endpoints**, не требующие аутентификации. Анализ класса `BinanceFuturesMarketData` (`bot/market_data.py`) показывает, что `AsyncClient` инициализируется с `api_key=None, api_secret=None`, что гарантирует невозможность отправки торговых приказов или доступа к приватным данным аккаунта. Это является фундаментальным архитектурным решением, полностью соответствующим концепции signal-only бота и обеспечивающим **нулевой риск несанкционированной торговли** при компрометации кода или конфигурации. Используемые публичные endpoints включают: `fapi.binance.com/fapi/v1/klines` (исторические свечи), `fapi.binance.com/fapi/v1/premiumIndex` (funding rate, mark price), `fapi.binance.com/futures/data/openInterestHist` (открытый интерес), а также WebSocket-потоки `wss://fstream.binance.com` для klines, bookTicker, markPrice и aggTrade.

Архитектура получения данных включает разумные механизмы защиты от перегрузки API: rate limiter на основе sliding window (`_SlidingWindowRateLimiter`) для endpoints с ограничениями по IP (`/futures/data/*`), circuit breaker для REST-операций (`_circuit_failures`, `_circuit_open_until`), и стратегию чанкирования подписок WebSocket (`subscribe_chunk_size`, `subscribe_chunk_delay_ms`). Это соответствует best practices для работы с публичными API Binance и снижает риск бана IP-адреса. Однако отсутствует явная обработка HTTP-заголовков `X-MBX-USED-WEIGHT-1M` и `Retry-After` при получении ответов 418/429 от REST API, хотя метод `_capture_retry_after` существует, его интеграция не гарантируется на всех путях выполнения. Для production-ready системы необходимо добавить централизованный обработчик ответов, который автоматически анализирует эти заголовки и применяет экспоненциальный backoff с джиттером перед следующим запросом.

### 1.2. Оценка доставки сигналов и Telegram-инфраструктуры

Подсистема доставки сигналов является одним из наиболее проработанных компонентов проекта и демонстрирует понимание требований production-ready signal-only системы. `TelegramQueue` (`bot/telegram/queue.py`) реализует **bounded priority queue** (`max_size=1000`) с четырьмя уровнями приоритета (`CRITICAL`, `HIGH`, `NORMAL`, `LOW`), rate limiting (20 сообщений/мин на чат, 1/сек глобально), дедупликацию по хешу содержимого (`dedup_window_seconds=60`) и автоматический retry с backoff. Это соответствует архитектуре крупных систем уведомлений и предотвращает flooding чата при всплеске сигналов. `TelegramSender` (`bot/telegram/sender.py`) дополнительно реализует Circuit Breaker (порог 5 ошибок, recovery 60 секунд), что защищает от бана со стороны Telegram API при временных сетевых проблемах.

Однако `TelegramQueue` использует **четыре отдельных `asyncio.Queue()`** по приоритетам вместо единой `asyncio.PriorityQueue`. Это создаёт потенциальную проблему starvation: если очередь `CRITICAL` постоянно заполнена, сообщения с приоритетом `LOW` (диагностика) могут никогда не быть обработаны. Хотя для signal-only бота это приемлемо (сигналы важнее диагностики), более элегантным решением была бы единая приоритетная очередь с возможностью задания весов. Также в `TelegramQueue` используется `datetime.utcnow()` для `created_at`, что является **deprecated** в Python 3.12+ и вызывает `DeprecationWarning` в Python 3.13. Согласно PEP 615, следует использовать `datetime.now(timezone.utc)` или `datetime.now(UTC)`. Метод `_get_next_message` обходит очереди в цикле `while True`, что может приводить к busy-waiting при пустых очередях. Оптимальнее использовать `asyncio.wait_for` или `asyncio.gather` с таймаутом.

### 1.3. Корректность трекинга сигналов (не позиций!)

Модуль `SignalTracker` (`bot/tracking.py`) корректно реализует жизненный цикл **сигнала**, а не позиции. Это подтверждается явной аннотацией `runtime_mode: "signal_only"` и `exchange_execution: False` в методе `to_log_row`. Трекер отслеживает переходы: `pending` (сигнал отправлен, ждёт активации ценой) -> `active` (цена достигла зоны входа) -> `tp1_hit` / `tp2_hit` / `sl_hit` / `invalidated`. Это точное отражение того, что должен делать signal-only бот — **мониторить качество сгенерированных рекомендаций**, а не управлять капиталом. Более того, трекер поддерживает batch-запись outcomes на диск через `MemoryRepository` и `asyncio.Lock`, что защищает от потери данных при сбоях.

Тем не менее, в `SignalTracker` присутствует архитектурная проблема: метод `_persist_features_store` выполняет **синхронную запись на диск** (`json.dumps` + `os.replace`) внутри async-приложения. Хотя запись осуществляется через `tempfile.NamedTemporaryFile` с атомарным `os.replace`, блокирующий файловый I/O в event loop может вызывать micro-freezes. Для production-ready системы это следует вынести в отдельный thread-пул через `asyncio.to_thread` или `loop.run_in_executor`. Также отсутствует механизм очистки старых записей в `features_store` — при длительной работе бота словарь будет неограниченно расти в памяти. Необходим TTL-based eviction или ограничение по количеству записей (LRU). Важно отметить, что `SignalTracker` не имеет понятия "корреляции сигналов" — если бот отправит 5 сигналов на покупку по разным альткоинам в момент резкого падения BTC, все 5 сигналов, вероятно, сработают по SL. Signal-only бот не управляет риском, но должен **предупреждать пользователя о высокой корреляции** в одном цикле.

## 2. Пересмотренные критические системные ошибки

### 2.1. Подавление ошибок в аналитическом конвейере — КРИТИЧНО

Хотя signal-only бот не рискует капиталом автоторговли, массовое подавление ошибок (`146 случаев except Exception`) остаётся **критической проблемой**, поскольку оно непосредственно деградирует качество аналитики. В signal-only системе основной "продукт" — это качество сигналов. Если стратегия `fvg_setup` падает с `ZeroDivisionError` или `KeyError` из-за изменения схемы данных, но ошибка подавляется через `LOG.exception` + `return None`, бот просто не генерирует сигнал вместо того, чтобы сообщить об аномалии. Это создаёт **скрытые пробелы в покрытии** — пользователь не знает, что какая-то пара или стратегия перестала работать. В отличие от торгового бота, где подавление ошибки может привести к убытку, здесь оно приводит к **снижению win rate** из-за необнаруженной деградации.

Анализ схожих проектов (CryptoSignal 5500★) показывает, что там используется строгий перехисключений с явным возвратом статуса ошибки в telemetry. Наш проект должен следовать той же модели: каждая стратегия должна возвращать не `Signal | None`, а `StrategyDecision` с полем `error`, которое агрегируется в `TelemetryStore` и отображается в `startup_reporter`. Если более 10% вызовов стратегии падают с ошибкой за час — это должно триггерить CRITICAL alert в Telegram. `EventBus._safe_call` также должен классифицировать ошибки: `ConnectionError` — retryable, `ValueError` — bug, `TypeError` — schema mismatch. Каждый тип требует разной реакции: retryable — exponential backoff, bug — alert разработчику, schema mismatch — graceful degradation с логированием.

### 2.2. Неограниченная очередь в EventBus и отсутствие backpressure

`EventBus` использует **неограниченную `asyncio.Queue`** для диспатчинга событий. В signal-only контексте это особенно опасно, поскольку бот обрабатывает WebSocket-потоки от десятков символов одновременно. При восстановлении WebSocket-соединения после разрыва Binance может отправить burst исторических сообщений (backfill), которые заполнят очередь мгновенно. Так как бот не торгует и не ограничен latency исполнения ордера, пользователь может подумать, что "бот просто немного отстаёт". Однако в реальности память будет расти, пока не исчерпается лимит процесса, что приведёт к **OOM-киллу** и полной остановке мониторинга.

Решение: внедрить **bounded queue** (`maxsize=10_000`) и политику **backpressure**: при заполнении очереди на 80% начинать отбрасывать устаревшие `KlineCloseEvent` (оставлять только последний для каждого symbol+interval). Это приемлемо для signal-only бота, поскольку он не торгует внутри бара и может позволить себе пропустить несколько промежуточных свечей. Для `AggTradeEvent` и `MarkPriceEvent` backpressure должен работать иначе: агрегировать данные (средняя цена, объём) вместо отбрасывания, чтобы не потерять информацию о ликвидациях. Согласно best practices из `carlosrod723/Binance-API-Trading-Bot`, production-grade системы используют "event history buffer" с фиксированным размером (например, 1000 событий) и circular eviction.

### 2.3. Блокировки из threading в async коде

Использование `threading.RLock()` в `_FrameCache` (`bot/market_data.py`, `bot/features.py`) остаётся архитектурной ошибкой даже для signal-only бота. Хотя нет позиций, которые нужно быстро обновлять, блокировка кэша фреймов данных всё равно приводит к **заморозке event loop** при одновременном доступе из WebSocket-обработчика (запись нового бара) и аналитического engine (чтение истории). В моменты высокой волатильности, когда Binance шлёт сотни сообщений в секунду, это может вызывать задержки обработки в несколько секунд, что приведёт к **устареванию сигналов** — бот отправит сигнал, основанный на данных 5-секундной давности, в то время как рынок уже резко сдвинулся. Замена на `asyncio.Lock` или использование lock-free структур (атомарные операции с `asyncio.Event`) решит проблему.

## 3. Аудит торговых стратегий (SMC/ICT) в Signal-Only контексте

### 3.1. Сравнение с эталонной библиотекой `smartmoneyconcepts`

Сравнение реализаций SMC-индикаторов в проекте с эталонной библиотекой `smartmoneyconcepts` (987 строк, 1754 total, joshyattridge) выявил **существенные расхождения в алгоритмах**, которые могут приводить к разным результатам.

**Fair Value Gap (FVG):**
- Наш проект (`bot/strategies/fvg.py`): `high[i-2] < low[i]` — проверяет только что high двух баров назад ниже low текущего бара. Это **формальная проверка "дырки"**, но не требует импульсного характера движения.
- Эталон (`smartmoneyconcepts/smc.py`): `high.shift(1) < low.shift(-1) & close > open` — проверяет, что high предыдущего бара ниже low следующего бара, И текущая свеча бычья. Это требует **центрального импульсного бара** (current candle) с большим телом.
- Разница: Наш алгоритм может генерировать FVG на боковом рынке с мелкими гэпами, где нет импульса. Эталонный алгоритм требует явного импульса, что соответствует SMC-концепции "displacement".
- Дополнительно: Эталонная библиотека отслеживает `MitigatedIndex` — индекс бара, при котором FVG был "закрыт" ценой. Наш проект не отслеживает mitigation для FVG, что не позволяет фильтровать "свежие" vs "закрытые" гэпы.

**Order Block (OB):**
- Наш проект (`bot/strategies/order_block.py`): Ищет "последнюю медвежью свечу перед бычьим импульсом" (`last_bearish_candle_before_bullish_impulse`). Это упрощённая эвристика.
- Эталон (`smartmoneyconcepts/smc.py`): Ищет opposing candle перед strong move с заданным `threshold` (по умолчанию 0.05). OB определяется как зона, откуда начался импульс >= threshold.
- Разница: Наш алгоритм не использует `threshold` для силы импульса и может выделять OB на мелких колебаниях. Эталон требует значительного движения (5%), что фильтрует шум.
- Дополнительно: Эталон отслеживает `MitigatedIndex` для OB. Наш проект отслеживает `_MAX_OB_AGE = 30` баров, но не проверяет, был ли OB уже смягчён. Это может приводить к сигналам на "мертвых" OB.

**Swing Highs/Lows:**
- Наш проект (`bot/features.py`): `_swing_points` использует `rolling_max`/`rolling_min` с window `n`. 
- Эталон: `swing_highs_lows` использует более сложный алгоритм с удалением consecutive highs/lows и проверкой на "настоящие" swing points.
- Разница: Наш алгоритм может генерировать "фальшивые" swing points внутри консолидации, что искажает BOS/ChoCH detection.

### 3.2. Отсутствие mitigation tracking — ключевой дефект для SMC

В SMC-концепции **mitigation (закрытие зоны ценой)** является критическим фактором валидности. Если Order Block или Fair Value Gap уже были протестированы ценой, они считаются "сработавшими" и больше не являются зонами интереса. Проект полностью игнорирует этот аспект:
- `OrderBlockSetup` проверяет только возраст (`_MAX_OB_AGE = 30`), но не то, был ли OB уже смягчён.
- `FVGSetup` не отслеживает, закрыт ли FVG.
- `LiquiditySweepSetup` проверяет "reclaim", но не отслеживает, был ли liquidity pool уже "вымыт" ранее.

Эталонная библиотека `smartmoneyconcepts` реализует mitigation tracking для всех паттернов (FVG, OB, Liquidity) через `MitigatedIndex`. Внедрение аналогичного механизма в проект позволит фильтровать "мертвые" зоны и существенно повысить качество сигналов. Согласно `eplanetbrokers.com/training/smart-money-concept`, "price often returns to fill these imbalances, acting like a magnet" — но только если эти зоны **ещё не были заполнены**. Signal-only бот должен исключать смягчённые зоны из рассмотрения, иначе он генерирует сигналы на уже "отработанных" уровнях.

### 3.3. Некорректный backtest engine для signal-only системы

Текущий `VectorizedBacktester` (`bot/backtest/engine.py`) реализует **equity-based backtest** с расчётом `position_leverage`, `gross_ret`, `net_ret`, `equity_curve`. Это **абсолютно неприменимо** для signal-only бота. Бот не торгует, не открывает позиций, не использует плечо. Backtest signal-only системы должен:
1. Прогонять каждый сгенерированный сигнал по историческим данным.
2. Проверять, достигла ли цена `entry_low` / `entry_high` (активация).
3. После активации — проверять достижение `tp1`, `tp2`, `stop`.
4. Считать: **hit rate TP1**, **hit rate TP2**, **SL rate**, **average R**, **max consecutive losses**, **time to TP/SL**.
5. НЕ считать equity curve, не использовать `position_leverage`, не моделировать комиссии (нет торговли!).

SUPPORTED_SETUPS ограничен двумя стратегиями: `{"ema_cross", "momentum_breakout"}`. Ни одна из 15 SMC/ICT стратегий не может быть протестирована. Это означает, что весь backtest-модуль **бесполезен для оценки основного функционала** бота. Для signal-only системы необходим **signal-oriented backtest engine**, который:
- Принимает на вход список `Signal` (исторические) + OHLCV данные.
- Симулирует жизненный цикл сигнала (pending -> active -> resolved).
- Возвращает `SignalBacktestResult` с метриками качества сигналов.
- Работает со всеми setup_id из конфигурации.

Референс: `vectorbt` имеет `signal` tooling (`vbt.signals`), а `polars_backtest_extension` поддерживает signal-based backtesting через Polars expressions. Проект должен использовать подход, аналогичный `CryptoSignal` — простой walk-forward: "сигнал сгенерирован на баре N, проверяем что произошло на барах N+1..N+M".

## 4. Аудит ML и Data Science компонентов для Signal Quality

### 4.1. ML-модели как фильтр качества сигналов

В signal-only контексте ML-модуль (`bot/ml/signal_classifier.py`) должен выполнять одну функцию: **бинарная классификация сигнала** ("хороший" vs "плохой") перед отправкой в Telegram. Вместо этого реализованы `_CentroidModel` и `_LinearFallbackModel`, которые являются тривиальными эвристиками, а не ML. Согласно научным исследованиям (Rehman et al., "Evaluating The Performance Of Cryptocurrency Trading Signals", 2024), для классификации качества сигналов наиболее эффективными являются **Random Forest** и **XGBoost**, достигающие accuracy 67-70% на криптовалютных данных. `_CentroidModel` не имеет предсказательной способности на сложных нелинейных зависимостях.

Более того, `pyproject.toml` декларирует зависимости `lightgbm>=4.5`, `xgboost>=2.1`, но они **не используются в коде**. Это создаёт архитектурный разрыв: либо ML-модуль не дописан, либо зависимости включены "на вырост". Для production-ready signal-only бота необходимо:
1. Удалить `_CentroidModel` и `_LinearFallbackModel`.
2. Реализовать `XGBoostClassifier` или `RandomForestClassifier` через `scikit-learn`.
3. Признаки: `atr_pct`, `volume_ratio20`, `rsi14`, `adx14`, `oi_change_24h`, `funding_rate`, `structure_clarity`, `regime_label`.
4. Целевая переменная: `1` если сигнал достиг TP1, `0` если сработал SL (без lookahead bias!).
5. Валидация: TimeSeriesSplit (не случайное разбиение!) с минимум 5 фолдами.

### 4.2. Look-ahead bias в обучении — неприемлемо даже для фильтра

Метод `generate_labels` в `MLTrainingPipeline` (`bot/ml/training_pipeline.py`) использует `close.shift(-horizon_bars)` для определения outcome. Это **прямой доступ к будущим данным** при обучении. Даже для signal-only бота это неприемлемо: фильтр будет иметь "сверхспособность" предсказывать будущее, и на backtestе покажет accuracy 90%+, но в реальной работе — 50% (случайное угадывание). 

Правильный подход для signal-only:
1. Генерировать сигнал на баре `t` с параметрами entry, stop, tp1.
2. Проверять outcome на истории: цена на барах `t+1..t+horizon` достигла tp1 или sl?
3. Лейбл: `1` (tp1 first), `0` (sl first), `None` (не достигнуто в пределах horizon).
4. Использовать ТОЛЬКО данные, доступные на баре `t` (все признаки с `shift(>=0)`).

Согласно исследованию Jaquart et al. (2021), даже state-of-the-art ML-модели достигают лишь 50-56% accuracy на крипто-предсказаниях без lookahead bias. Любая модель с accuracy >75% на backtestе — почти наверняка содержит утечку данных.

### 4.3. Некорректная методология валидации

`WalkForwardOptimizer` (`bot/learning/walk_forward_optimizer.py`) использует `min_fold_size = 20` — слишком мало для статистически значимых результатов. Для signal-only бота нужно минимум **100-200 сигналов на фолд** (согласно Pardo, 1992). Формула expectancy некорректна: она включает убыточные сделки в `avg_r` с R=-1.0, что искажает расчёт. Для signal-only правильная метрика: **Signal Expectancy = (Win% × Avg TP1 R) - (Loss% × 1.0)**.

## 5. Инфраструктурные и Windows-специфичные проблемы

### 5.1. Зависимости и Python 3.13

`pyproject.toml` декларирует `pandas>=2.1.0` в обязательных зависимостях, что конфликтует с `Polars-first` подходом. Для signal-only бота, который не использует pandas в runtime (все стратегии на Polars), `pandas` должен быть перенесён в `optional` или `dev`. `requirements.txt` содержит `numpy<2.5`, что может конфликтовать с `polars>=1.39.0` на Python 3.13. `toml>=0.10.2` избыточен для Python 3.13+ (есть `tomllib` в stdlib). Рекомендуется: унифицировать все зависимости в `pyproject.toml`, удалить `requirements.txt` и `requirements-modern.txt`, использовать `uv` или `pdm` для lock-файла.

### 5.2. Windows 11 совместимость

`Makefile` содержит Unix-команды (`rm`, `cp`, `mkdir -p`) и бесполезен на Windows без GNU Make. `run_30min_test.bat` — простой `python main.py`, не покрывает тесты, линтинг, очистку. Необходимо:
1. Добавить `tox.ini` или `noxfile.py` для кроссплатформенного тестирования.
2. Добавить PowerShell-скрипты (`build.ps1`, `test.ps1`) для Windows-разработчиков.
3. Убедиться, что все пути используют `pathlib.Path` (в проекте в основном так и есть, хорошо).
4. Заменить `datetime.now()` на `datetime.now(timezone.utc)` во всех модулях (на Windows timezone может быть сложнее, чем на Linux).

### 5.3. Качество тестов

Тесты (`tests/`) покрывают только `ema_cross` и `momentum_breakout`. **Ни один из 15 SMC/ICT стратегий не имеет unit-теста.** Для signal-only бота это критично: каждая стратегия должна иметь тест с synthetic data, где известен ожидаемый сигнал. Например:
```python
def test_fvg_bullish():
    df = synthetic_ohlcv_with_fvg()  # 3 candles: high=100, gap candle, low=105
    signal = FVGSetup().detect(prepared)
    assert signal.direction == "long"
    assert signal.entry_low == 100.0
    assert signal.entry_high == 105.0
```

Отсутствуют property-based тесты (`hypothesis`) для инвариантов: `entry_low < entry_high`, `stop < entry_low` для long, `tp1 > entry_high` для long. Отсутствуют мок-тесты для Binance WebSocket (`respx`, `aioresponses`).

## 6. Полный список задач для Codex (Production-Ready Roadmap)

### P0 — Критические (безопасность и корректность)

| # | Задача | Файлы | Описание |
|---|--------|-------|----------|
| 1 | **Strict Exception Handling** | `bot/strategies/*.py`, `bot/core/event_bus.py` | Заменить все `except Exception: pass` на перехисключений по типам. Добавить `TelemetryStore` агрегацию ошибок по стратегиям. CRITICAL alert если >10% вызовов падает за час. |
| 2 | **Bounded EventBus Queue** | `bot/core/event_bus.py` | Заменить `asyncio.Queue()` на `asyncio.Queue(maxsize=10_000)`. Реализовать backpressure: при заполнении 80% отбрасывать устаревшие `KlineCloseEvent`, агрегировать `AggTradeEvent`. |
| 3 | **Async Locks** | `bot/market_data.py`, `bot/features.py` | Заменить `threading.RLock()` на `asyncio.Lock()` в `_FrameCache`. Для синхронного I/O использовать `asyncio.to_thread`. |
| 4 | **Remove Look-ahead Bias** | `bot/ml/training_pipeline.py` | Переписать `generate_labels`: outcome определяется по истории `t+1..t+horizon`, не `close.shift(-horizon)`. Все признаки — только `shift(>=0)`. |
| 5 | **Signal-Oriented Backtest** | `bot/backtest/engine.py` | Переписать `VectorizedBacktester` для signal-only: вход — список `Signal` + OHLCV. Проверка активации и достижения TP/SL. Метрики: hit rate, avg R, max consecutive losses. Поддержка всех 15 setup_id. |
| 6 | **Align FVG/OB with smartmoneyconcepts** | `bot/strategies/fvg.py`, `bot/strategies/order_block.py` | Исправить FVG: требовать импульсный central candle (`close > open`). Добавить `MitigatedIndex` tracking. OB: добавить threshold для силы импульса. Убрать фиксированный `_MAX_OB_AGE`, заменить на mitigation check. |

### P1 — Высокие (качество и производительность)

| # | Задача | Файлы | Описание |
|---|--------|-------|----------|
| 7 | **Unified Dependencies** | `pyproject.toml`, `requirements*.txt` | Удалить `requirements.txt` и `requirements-modern.txt`. Убрать `pandas` из обязательных зависимостей (в `optional`). Установить `python-binance>=1.0.0`. Убрать `toml` (использовать `tomllib` для 3.13+). Добавить `uv.lock` или `pdm.lock`. |
| 8 | **Real ML Classifier** | `bot/ml/signal_classifier.py` | Удалить `_CentroidModel`. Реализовать `XGBoostSignalFilter` через `xgboost.XGBClassifier` или `sklearn.ensemble.RandomForestClassifier`. Признаки: все из `SignalFeatures`. Валидация: `TimeSeriesSplit(n_splits=5)`. Сохранение/загрузка модели через `joblib`. |
| 9 | **Adaptive Scoring** | `bot/scoring.py` | Заменить фиксированные веса (`0.70`, `0.30`) на адаптивные через rolling correlation. Нормализовать `volume_ratio20` через Z-score вместо деления на 2.5. Добавить `funding_rate` в scoring с адаптивными порогами (rolling percentiles). |
| 10 | **Mitigation Tracking** | `bot/strategies/fvg.py`, `bot/strategies/order_block.py`, `bot/strategies/liquidity_sweep.py` | Добавить поле `mitigated_at: datetime | None` в `Signal`. Фильтровать сигналы на смягчённых зонах. Использовать алгоритм из `smartmoneyconcepts` для отслеживания. |
| 11 | **Windows Support** | `Makefile`, новые файлы | Добавить `tox.ini` с envlist `py313`. Добавить `scripts/build.ps1`, `scripts/test.ps1` для Windows. Убедиться, что все `datetime.utcnow()` заменены на `datetime.now(timezone.utc)`. |
| 12 | **Unit Tests for SMC** | `tests/test_strategies.py` | Написать тесты для каждой из 15 стратегий с synthetic OHLCV, где паттерн гарантированно присутствует. Использовать `hypothesis` для property-based testing инвариантов (`entry_low < entry_high`, `stop < entry_low` для long). |

### P2 — Средние (надёжность и мониторинг)

| # | Задача | Файлы | Описание |
|---|--------|-------|----------|
| 13 | **Remove print()** | Все `*.py` | Заменить 186 вызовов `print()` на `LOG.info/debug`. Убрать `print()` из `smartmoneyconcepts/__init__.py` (credit message). |
| 14 | **Async Features Store** | `bot/tracking.py` | Вынести `_persist_features_store` в `asyncio.to_thread`. Добавить LRU eviction с `maxsize=10_000` записей. |
| 15 | **Binance API Headers** | `bot/market_data.py` | Добавить централизованный обработчик HTTP-ответов: парсить `X-MBX-USED-WEIGHT-1M`, `Retry-After`. Автоматический exponential backoff при 418/429. |
| 16 | **Pre-commit Hooks** | `.pre-commit-config.yaml` | Добавить `ruff`, `mypy`, `pytest`. Запретить `# type: ignore` без justification-комментария. |
| 17 | **Graceful Shutdown** | `bot/cli.py`, `bot/application/bot.py` | Обработка `SIGINT`/`SIGTERM`: закрыть WebSocket, flush `TelegramQueue`, сохранить `features_store`, сделать `await memory_repo.close()`. |
| 18 | **Signal Correlation Warning** | `bot/application/bot.py` | Если в одном цикле >3 сигнала в одном направлении по разным активам — добавить warning в Telegram: "Высокая корреляция сигналов, рыночный риск повышен". |

### P3 — Низкие (UX и документация)

| # | Задача | Файлы | Описание |
|---|--------|-------|----------|
| 19 | **Docker Support** | `Dockerfile`, `docker-compose.yml` | Мультистейдж Dockerfile на базе `python:3.13-slim`. docker-compose с healthcheck. |
| 20 | **GitHub Actions CI** | `.github/workflows/ci.yml` | Запуск `tox`, `pytest`, `ruff`, `mypy` на Python 3.13. Matrix: ubuntu-latest, windows-latest. |
| 21 | **README v2** | `README.md` | Документация для Windows 11: PowerShell setup, `uv` installation, запуск без `make`. |
| 22 | **Strategy Documentation** | `docs/STRATEGIES.md` | Для каждой из 15 стратегий: описание, параметры, пример synthetic data, ссылка на соответствующую функцию `smartmoneyconcepts`. |

## 7. Заключение

С учётом signal-only природы бота, проект демонстрирует **смешанное качество**: с одной стороны, правильно реализованы публичное API-взаимодействие, Telegram-доставка с rate limiting и circuit breaker, а также корректный трекинг жизненного цикла сигнала. С другой стороны, критические системные ошибки (подавление исключений, неограниченные очереди, thread-блокировки в async) и глубокие логические дефекты в аналитическом ядре (некорректные SMC-алгоритмы, отсутствие mitigation tracking, equity-based backtest для signal-only системы, псевдо-ML с look-ahead bias) делают бота **непригодным для production** в текущем виде.

После исправления P0-задач бот станет **стабильным и безопасным** с точки зрения инфраструктуры. После исправления P1 — **качественным аналитическим инструментом** с корректными SMC-индикаторами и научно обоснованным ML-фильтром. P2 и P3 — полировка до уровня professional open-source project.

**Рекомендуемый приоритет:** Начать с P0 (tasks 1-6) и P1 (tasks 7-12) — они обеспечивают фундаментальную корректность. P2 и P3 можно отложить на второй этап.
