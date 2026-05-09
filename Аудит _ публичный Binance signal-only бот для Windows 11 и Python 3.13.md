# Аудит `Crypto-Analytic-Signal-Bot`: публичный Binance signal-only бот для Windows 11 и Python 3.13

Дата аудита: **08 мая 2026**. Репозиторий: [`TrololoBird/Crypto-Analytic-Signal-Bot`](https://github.com/TrololoBird/Crypto-Analytic-Signal-Bot). Целевая среда, по вашему требованию: **Windows 11**, **Python 3.13**, **без автоторговли**, **без Binance-авторизации**, только **публичные market-data endpoints** и генерация аналитики/сигналов.

> Главный вывод: проект заметно продвинулся в правильную сторону. В коде уже есть явная политика public-only REST/WS, разделение WebSocket endpoint-ов, кэширование enrichment-данных, shortlist-refresh с fallback-режимами и отдельный signal-only пример конфигурации. Но проект всё ещё выглядит как активно сгенерированная ИИ-система: слишком много сложной логики собрано в крупных файлах, часть стратегий похожа на экспериментальные/roadmap-эвристики, зависимости для обычного signal-only запуска смешаны с dev/live/ML/regime пакетами, а проверяемая среда не совпадает с заявленной Python 3.13 + Windows 11. Перед реальным постоянным запуском нужно стабилизировать public endpoint boundary, WebSocket routing, contracts данных, dependency profile и тестовую матрицу.

## 1. Объём проверки и важные ограничения

Я повторно получил актуальную версию репозитория через GitHub CLI, зафиксировал структуру проекта, прогнал статический обзор файлов, вручную разобрал критические подсистемы и сверил Binance REST/WS политику с официальной документацией Binance USD-M Futures. Внутри репозитория были отдельно просмотрены конфигурации, WebSocket-слой, REST-клиент, runtime validation, shortlist/universe, подготовка feature/indicator данных, engine/registry стратегий, фильтры, strategy asset-fit и зависимости.

| Зона аудита | Что проверялось | Итоговая оценка |
|---|---|---|
| Public-only posture | REST allowlist, WS stream классификация, запреты private/auth/trading markers | **Хорошая база, но нужен отдельный security/public-boundary test suite в CI** |
| Binance REST | `/fapi/v1/*`, `/futures/data/*`, weight/IP limiter, enrichment cache | **В целом корректно, но риск burst-режимов и деградации enrichment при массовом shortlist** |
| Binance WebSocket | split `public`/`market`, dynamic subscribe, reconnect, silence monitor, backfill | **Архитектура правильная, но нужны live-smoke тесты против testnet/mainnet public endpoints** |
| Стратегии | registry, roadmap strategies, asset-fit, signal engine | **Слишком много эвристик и roadmap-логики; нужен карантин экспериментальных стратегий** |
| Фильтры | RR/ATR/spread/funding/OI/bias gates, ML gate | **Фильтры полезные, но требуют унификации причин rejection и строгого контракта входных данных** |
| Shortlist | REST full refresh, WS light refresh, cached/pinned fallback | **Хорошо продуманная fallback-модель, но нужно усилить телеметрию качества и freshness** |
| Индикаторы/данные | `PreparedSymbol`, feature columns, advanced fallback | **Функционально мощно, но монолитно; риск silent fallback и NaN/None propagation** |
| Python 3.13 / Windows 11 | `pyproject`, `requirements`, CI, runtime entrypoint | **Заявлено Python 3.13, но CI/локальная проверка не доказывают Windows 11 совместимость** |

Текущая песочница содержит Python 3.11, поэтому это не заменяет полноценный запуск на Python 3.13/Windows 11. При этом лог проверки качества показал, что в текущей среде не установлены `ruff` и `pytest`, поэтому фактический тестовый прогон не состоялся: `No module named ruff`, `No module named pytest`. Это не означает, что тесты падают; это означает, что текущая проверка окружения **не подтверждает** работоспособность набора тестов.

## 2. Критические выводы по public-only и отсутствию автоторговли

В проекте уже есть правильный архитектурный сдвиг к **жёсткому public-only boundary**. REST-клиент содержит реестр разрешённых endpoint-ов в `bot/market_data.py`: `exchangeInfo`, `ticker/24hr`, `klines`, `premiumIndex`, `openInterest`, `fundingRate`, `aggTrades` и `/futures/data/*` endpoints для open-interest/long-short/basis аналитики. В этом же файле видны `_PUBLIC_PATH_PREFIXES`, `_ALLOWED_PUBLIC_REST_PATHS` и `_FORBIDDEN_PUBLIC_PATH_MARKERS`, то есть код не должен свободно ходить по произвольным Binance path-ам.

WebSocket-слой также явно запрещает private/auth streams: в `bot/websocket/subscriptions.py` функция `stream_endpoint_class()` проверяет markers вроде `listenkey`, `/private`, `userdatastream`, `@account`, `@order` и выбрасывает `ValueError`. Это правильная защита от случайного внедрения user-data stream и приватных событий.

| Компонент | Наблюдение | Риск | Исправление |
|---|---|---:|---|
| `bot/market_data.py` | Есть allowlist публичных REST endpoint-ов | Низкий | Оставить allowlist единственным способом вызова Binance REST |
| `bot/websocket/subscriptions.py` | Private/auth WS markers запрещены | Низкий | Добавить regression-тесты на все запрещённые stream names |
| `bot/config.py` | Runtime validation запрещает private/auth/trading markers в URL/path | Средний | В CI проверять все `.toml.example` и реальные config templates |
| Документация | Может отставать от кода | Средний | README должен ссылаться на code policy, а не вручную дублировать endpoint list |
| Архитектура | Нет автоторговли как целевого режима | Средний | Удалить/изолировать любые будущие trading abstractions до отдельного пакета, если они появятся |

Важно: даже если сейчас торговые endpoint-ы не используются, нужно формально закрепить invariant: **в production signal-only сборке не должно быть кода, который способен подписаться на listenKey, создать order, отменить order, читать account или position risk**. Не достаточно комментариев в README; это должно быть enforced тестами.

## 3. Binance WebSocket: сильные стороны и проблемы

Официальная документация Binance USD-M Futures указывает, что WebSocket market streams обслуживаются через `wss://fstream.binance.com`, а новые разделённые endpoints используют routed paths вроде `/ws-fapi/v1`, `/ws-fapi/v1/public`, `/ws-fapi/v1/private` и `/ws-fapi/v1/market`, при этом для публичного market-data режима private endpoint не нужен.[^binance-ws][^binance-ws-change]

В проекте endpoint split реализован логично: `bookTicker` относится к `public`, а klines, `aggTrade`, `!markPrice@arr`, `!forceOrder@arr`, `!miniTicker@arr` относятся к `market`. Intended streams разделены в `manager._intended_streams_by_endpoint["public"]` и `["market"]`, после чего `resubscribe_all()` отправляет JSON `SUBSCRIBE` командами chunks. В `bot/websocket/connection.py` URL строится через `endpoint_base_url(endpoint)` и затем нормализуется к `/stream`, что похоже на режим dynamic subscription через combined-stream endpoint.

> Основной риск WebSocket сейчас не в том, что архитектура неправильная, а в том, что она стала достаточно сложной и требует **обязательного live-smoke теста** против реальных публичных Binance endpoints. Unit-тесты проверяют resubscribe/reconnect/backfill, но не доказывают, что итоговый URL и JSON subscription действительно принимаются текущей Binance инфраструктурой.

| Проблема | Где видно | Почему важно | Приоритет |
|---|---|---|---:|
| Нет обязательного live-smoke для routed WS URL | `connection.build_stream_url()`, `subscriptions.resubscribe_all()` | Binance меняла WebSocket routing; unit-тесты могут пройти при неверном фактическом URL | **P0** |
| Client ping каждые 20 секунд | `websockets.connect(... ping_interval=20.0 ...)` | Не критично, но Binance сама шлёт ping; лишние client ping/pong увеличивают control traffic | P2 |
| Dynamic SUBSCRIBE chunks есть, но лимит входящих сообщений надо тестировать интеграционно | `subscribe_chunk_size`, `subscribe_chunk_delay_ms` | Binance ограничивает входящие control messages; ошибка chunk/pacing может привести к disconnect | **P1** |
| Stream count warning начинается после 120/200 streams | `run_stream_session`, `resubscribe_all()` | При большом shortlist легко получить сотни streams, особенно если включить klines+aggTrade | **P1** |
| `!forceOrder@arr` глобальный stream | `global_streams()` | Это публичный stream, но шумный; может сильно нагружать обработку без прямой пользы каждому символу | P2 |

Рекомендация: добавить отдельный `tests_live/test_binance_ws_public_smoke.py`, выключенный по умолчанию и запускаемый только при `RUN_LIVE_BINANCE_TESTS=1`. Он должен проверять минимальный набор: подключиться к `public`, подписаться на `btcusdt@bookTicker`; подключиться к `market`, подписаться на `btcusdt@kline_1m`; получить хотя бы одно сообщение за 20–30 секунд; закрыть соединение. Этот тест должен использовать **только публичные endpoint-ы** и не требовать ключей.

## 4. Binance REST API и endpoints

REST-слой выглядит одним из наиболее зрелых участков проекта. В `bot/market_data.py` явно задан реестр `_PUBLIC_ENDPOINT_REGISTRY`, включающий `/fapi/v1/exchangeInfo`, `/fapi/v1/ticker/24hr`, `/fapi/v1/klines`, `/fapi/v1/premiumIndex`, `/fapi/v1/openInterest`, `/fapi/v1/fundingRate`, `/fapi/v1/aggTrades` и публичные `/futures/data/*` endpoints. Это соответствует назначению market-data analytics без авторизации.[^binance-rest][^binance-long-short]

Отдельно положительно, что проект различает **request weight** и **IP-limited endpoints**. Для `/futures/data/*` в коде отмечено, что вес может быть `0`, но действует отдельный IP cap. В тестах `test_market_data_limits.py` уже проверяется, что `futures_data_request_limit_per_5m` clamp-ится до 1000 и что kline weight зависит от `limit` tier. Это правильная модель для Binance USD-M.

| Зона REST | Что хорошо | Что нужно улучшить |
|---|---|---|
| Endpoint allowlist | Нельзя случайно вызвать произвольный path | Добавить тест, который перебирает весь `_PUBLIC_ENDPOINT_REGISTRY` и сверяет отсутствие private/account/order markers |
| Weight estimation | Есть tiered kline estimate и отдельные weights | Вынести веса в `EndpointSpec` полностью, чтобы не было расхождения между registry и `_ENDPOINT_WEIGHTS` |
| IP limiter | Есть sliding-window limiter для `/futures/data/*` | Делать лимиты per endpoint-group: funding, futures-data, klines, generic request weight |
| Error handling | Есть обработка 429/418 и session lifecycle | Добавить circuit breaker для повторных 418/429, чтобы бот сам переходил в degraded mode |
| Enrichment data | Funding/OI/LS/basis используются в аналитике | Ввести TTL/freshness status в каждый enrichment field, а не просто значение |

Ключевая архитектурная рекомендация: сделать единый `EndpointCatalog`, где каждый endpoint имеет `name`, `path`, `method`, `public=True`, `security_type="NONE"`, `weight_policy`, `ip_limit_policy`, `ttl`, `cache_key`, `used_by`. Сейчас часть этой информации уже есть, но она распределена по registry, комментариям, helper-функциям и тестам. Для ИИ-сгенерированного проекта это опасно: при будущих правках модель может добавить новый вызов в обход одного из слоёв.

## 5. Shortlist, universe и обработка market-data

Shortlist-сервис стал заметно лучше, чем типичная ИИ-заготовка. В `bot/application/shortlist_service.py` есть staged refresh: сначала pinned fallback, затем `ws_light`, cached shortlist и `rest_full`. Логика `source` и `fallback_reason` аккуратно обновляется: `ws_light`, `cached`, `rest_full`, `pinned_fallback`. Это хорошая основа для живого signal-only режима, потому что она позволяет не останавливать бота при холодном WS cache или временной REST ошибке.

Однако shortlist сейчас нужно рассматривать как **critical control plane**: от него зависит, какие символы получают WebSocket streams, какие symbols анализируются, и насколько быстро бот реагирует на смену ликвидности/волатильности. Поэтому простого ranking score недостаточно; нужна диагностируемость качества.

| Риск shortlist | Проявление | Последствие | Рекомендация |
|---|---|---|---|
| Silent degradation в pinned/cached | Fallback выглядит штатно, но качество сигналов может упасть | Бот анализирует устаревший/нерелевантный universe | Добавить alert/metric при `pinned_fallback` дольше N refresh cycles |
| Freshness смешан с scoring | Ticker/book/mark/OI/funding freshness участвуют в rank, но не всегда как hard gate | В shortlist могут попасть symbols с частично устаревшими enrichment полями | Разделить `eligibility gates` и `ranking score` |
| REST full refresh может быть дорогим | Много enrichment endpoints на большой universe | Риск 429 и задержки старта | Ввести batch budget planner: сколько REST calls требуется до запуска |
| WS light зависит от warm cache | При cold start shortlist может долго жить на pinned | Медленный выход на реальный рынок | Явно логировать time-to-live shortlist и cache warm percentage |

Хорошее направление развития: shortlist должен возвращать не просто список `UniverseSymbol`, а структуру `ShortlistSnapshot`, где есть `symbols`, `source`, `generated_at`, `data_age_stats`, `coverage`, `fallback_reason`, `rest_budget_used`, `ws_cache_coverage`, `excluded_counts_by_reason`.

## 6. Индикаторы, подготовка данных и feature pipeline

`bot/features.py` выполняет слишком много ролей одновременно: подготовка OHLCV, расчёт индикаторов, advanced fallback, сбор feature columns, кэширование и создание `PreparedSymbol`. Это типичный риск ИИ-сгенерированной архитектуры: файл функционально богатый, но становится трудно доказать, что каждый downstream strategy получает валидный контракт.

Главная логическая проблема здесь — не конкретная формула RSI/ATR/ADX, а отсутствие достаточно строгой **schema/freshness validation** после каждого этапа. Когда индикаторы, enrichment и fallback смешаны в одном pipeline, стратегия может получить значение, которое синтаксически есть, но семантически устарело, рассчитано fallback-методом или содержит слишком много NaN.

| Что нужно ввести | Зачем |
|---|---|
| `FeatureSchema` с обязательными колонками, типами и допустимой долей NaN | Чтобы стратегия не работала на частично испорченном фрейме |
| `IndicatorQualityReport` | Чтобы понимать, какие индикаторы рассчитаны нативно, а какие fallback-путём |
| `DataFreshness` для OHLCV, book, mark, OI, funding, LS ratio | Чтобы фильтры могли reject-ить сигнал не только по значениям, но и по качеству данных |
| Минимальные тестовые fixtures на 50/100/300 свечей | Чтобы выявлять off-by-one и warmup ошибки индикаторов |
| Разделение modules: `ohlcv.py`, `indicators.py`, `enrichment.py`, `prepared_symbol.py` | Чтобы снизить риск случайных правок в монолите |

Практически: перед любым `SignalEngine.analyze()` должен быть hard gate: `prepared.quality.is_usable_for(setup_id)`. Если стратегия требует OI/funding/basis, отсутствие свежего значения должно давать не “нейтральный score”, а явную причину `data_missing.open_interest` или `data_stale.funding_rate`.

## 7. Стратегии и engine

`bot/core/engine/engine.py`, `bot/core/engine/registry.py`, `bot/strategy_asset_fit.py` и `bot/strategies/roadmap.py` образуют сложную систему выбора стратегий. Положительно, что появились asset-fit profiles и тесты на соответствие registered setup IDs. Это снижает риск запуска стратегии на неподходящем активе, например BTC-correlation на самом BTC или altcoin-season logic на ETH.

Но `roadmap.py` по смыслу содержит много экспериментальных стратегий и эвристик: OI divergence, BTC correlation, altcoin season index, long/short ratios, whale walls и др. Такие стратегии могут быть полезны, но их нельзя смешивать с production-ready сигналами без статуса зрелости.

| Проблема | Почему это опасно | Решение |
|---|---|---|
| Roadmap-стратегии могут выглядеть как полноценные production setups | Пользователь получает сигнал с красивым названием, но слабой статистической валидацией | Ввести `strategy_status`: `production`, `beta`, `experimental`, `disabled` |
| Единый score между разными стратегиями может быть несравним | Breakout score, funding reversal score и correlation score имеют разную природу | Нормализовать score per setup и хранить calibration metadata |
| Asset-fit решает применимость, но не доказывает качество данных | Strategy может быть применима к активу, но enrichment stale | Объединить asset-fit и data-fit в общий precheck |
| Ошибки strategy_id/setup_id критичны | Registry/profile/test рассинхрон ломает маршрутизацию | Оставить parity-тесты обязательными в CI |

Рекомендация: сделать для каждой стратегии `StrategyManifest`:

| Поле | Пример |
|---|---|
| `setup_id` | `funding_reversal` |
| `status` | `production` / `beta` / `experimental` |
| `required_timeframes` | `1m`, `5m`, `15m` |
| `required_features` | `atr`, `adx`, `rsi`, `volume_zscore` |
| `required_enrichment` | `funding_rate`, `open_interest`, `basis` |
| `min_history_bars` | `200` |
| `allowed_assets` | `BTC`, `ETH`, `ALT`, `METAL` |
| `score_calibration` | `0..1`, `empirical`, `heuristic` |
| `risk_profile` | `trend`, `mean_reversion`, `event`, `microstructure` |

Такой manifest сделает engine предсказуемым: сначала проверяется manifest, затем data-fit, затем signal detect, затем фильтры.

## 8. Фильтры сигналов

`bot/filters.py` выполняет важную роль финального gate: RR, spread, ATR, funding, OI, ML gate и bias penalties. Это правильно, потому что signal-only бот должен быть скорее консервативным, чем шумным.

Основной риск — фильтры должны быть полностью объяснимыми. Если сигнал отклонён, нужно видеть не только `False`, а цепочку причин: `spread.too_wide`, `rr.too_low`, `funding.extreme_against_direction`, `oi_missing`, `adx_weak`, `ml_rejected`. Это особенно важно для активной разработки: иначе вы будете менять стратегию, хотя сигнал на самом деле режет фильтр.

| Улучшение | Эффект |
|---|---|
| Единый enum `RejectReason` | Уменьшит хаос строковых причин |
| `FilterDecision` с `passed`, `score_delta`, `hard_rejects`, `soft_penalties` | Сделает фильтрацию объяснимой |
| Per-setup threshold registry | Уберёт магические значения из общего потока |
| Telemetry по reject reasons | Покажет, какие фильтры слишком агрессивны |
| Backtest/replay на reject distribution | Поможет калибровать сигналы без автоторговли |

Для signal-only режима особенно важно: фильтр не должен “молчаливо ухудшать confidence”. Если risk/reward или spread плохой, лучше отклонить сигнал явно, чем отправить пользователю “слабый” сигнал, который выглядит как рекомендация.

## 9. Python зависимости, Python 3.13 и Windows 11

`pyproject.toml` объявляет `requires-python = ">=3.13,<3.14"`, что соответствует вашему целевому запуску. Runtime-зависимости включают `polars>=1.40.1`, `aiohttp>=3.13.5`, `numpy>=2.4.4`, `aiogram>=3.27.0`, `websockets>=16.0,<17.0`, `aiosqlite>=0.22.1`, `msgspec>=0.21.1`, `tenacity>=9.1.4`, `structlog>=25.5.0`, `pydantic>=2.12.5,<2.13`.

Проблема в том, что `requirements.txt` смешивает всё: runtime, dev, tests, dashboard/live, regime modeling и ML/training. Для Windows 11 signal-only установки это плохая практика. Пользователь, который просто хочет запустить аналитического Telegram/console бота, будет тянуть `hmmlearn`, `scikit-learn`, `statsmodels`, `lightgbm`, `xgboost`, `optuna`, `pyarrow`, `pandas`, `fastapi`, `uvicorn`, `polars_ta`. Это увеличивает риск проблем с wheel-ами, временем установки и конфликтами, хотя для базового signal-only режима всё это не обязательно.

| Файл | Проблема | Рекомендация |
|---|---|---|
| `requirements.txt` | Смешаны все extras | Разделить на `requirements/runtime.txt`, `dev.txt`, `test.txt`, `live.txt`, `ml.txt`, `regime.txt` |
| `pyproject.toml` | Хорошо описывает extras, но `requirements.txt` этому противоречит | Сделать `requirements.txt` минимальным или удалить в пользу `pip install .[test]` |
| CI | Не доказывает Windows 11 + Python 3.13, если нет matrix | Добавить GitHub Actions matrix: `windows-latest`, `ubuntu-latest`, Python `3.13` |
| Quality tools | В текущей среде `ruff`/`pytest` отсутствовали | Документировать `pip install -e .[dev,test]` как обязательный dev setup |
| Pydantic pin | `<2.13` может быстро устареть | Если нет конкретного бага, использовать `<3` и покрывать config tests |

Для production signal-only на Windows 11 я бы рекомендовал такой набор:

```text
python-dotenv>=1.2.2
polars>=1.40.1
aiohttp>=3.13.5
numpy>=2.4.4
aiogram>=3.27.0
websockets>=16.0,<17.0
aiosqlite>=0.22.1
msgspec>=0.21.1
tenacity>=9.1.4
structlog>=25.5.0
pydantic>=2.12.5,<3
```

Всё, что относится к dashboard, ML, regime, backtest-heavy workflows и live observability, должно ставиться отдельными extras. Это особенно важно для Windows: чем меньше compiled/scientific packages в базовой установке, тем меньше ложных проблем у пользователя.

## 10. Windows 11 runtime

В `bot/cli.py` уже есть Windows-related поведение: event-loop policy, runtime lifecycle, shutdown/status/stop/backtest/replay команды. Это хорошо, но нужно закрепить именно Windows 11 эксплуатацию.

| Риск Windows | Почему важно | Что сделать |
|---|---|---|
| Event loop differences | `aiohttp`, `websockets`, signal handling и subprocess поведение отличаются от Linux | Иметь CI smoke на `windows-latest` |
| File locks/PID lock | Windows иначе обрабатывает locked files | Добавить тесты start/stop/status под Windows |
| Paths/log rotation | `\` paths, encoding, console codepage | Принудительно UTF-8 logging и pathlib everywhere |
| Long-running process | Ctrl+C/service startup отличаются | Документировать запуск через PowerShell и Task Scheduler/NSSM |
| SQLite | Windows file locking может быть заметнее | Настроить busy timeout и WAL, если используется persistent cache |

Минимальная проверка перед релизом: `py -3.13 -m venv .venv`, `..venv\Scripts\python -m pip install -U pip`, `..venv\Scripts\pip install -e .[test]`, `..venv\Scripts\pytest`, затем live-smoke с публичными Binance endpoints.

## 11. Приоритетный план исправлений

| Приоритет | Исправление | Ожидаемый эффект |
|---:|---|---|
| **P0** | Добавить CI matrix `windows-latest` + Python 3.13 + `pip install -e .[test]` + full pytest | Появится доказательство совместимости с целевой средой |
| **P0** | Добавить live-smoke тесты Binance REST/WS public-only под env flag | Быстро выявит сломанные endpoint URL, route changes и subscription формат |
| **P0** | Ввести public-boundary tests: запрет `/private`, `listenKey`, `/order`, `/account`, `/positionRisk`, signed/security endpoints | Защитит проект от случайного автотрейдинга/авторизации |
| **P1** | Разделить dependencies на runtime/dev/test/live/ml/regime | Упростит Windows установку и снизит конфликты |
| **P1** | Ввести `EndpointCatalog` для REST и WS | Уберёт расхождение между registry, weights, TTL и tests |
| **P1** | Ввести `StrategyManifest` и статусы стратегий | Отделит production-сигналы от roadmap/experimental логики |
| **P1** | Ввести `PreparedSymbolQuality` / `FeatureSchema` / `DataFreshness` | Стратегии перестанут работать на некачественных/устаревших данных |
| **P1** | Унифицировать `RejectReason` и telemetry фильтров | Улучшит отладку и калибровку сигналов |
| P2 | Оптимизировать WS ping policy и stream count budget | Снизит риск disconnect при больших shortlist |
| P2 | Вынести `features.py` на несколько modules | Улучшит поддерживаемость и уменьшит риск ИИ-регрессий |

## 12. Рекомендуемая целевая архитектура

Текущую архитектуру лучше стабилизировать вокруг пяти контрактов: `EndpointCatalog`, `MarketDataSnapshot`, `PreparedSymbol`, `StrategyManifest`, `SignalDecision`. Тогда каждый слой будет проверяемым отдельно.

| Слой | Ответственность | Что не должен делать |
|---|---|---|
| EndpointCatalog | Разрешённые публичные REST/WS endpoints, лимиты, TTL | Не должен знать стратегии |
| MarketDataClient | Получение и кэширование данных Binance | Не должен принимать произвольные URL/path |
| ShortlistService | Выбор universe на основе fresh market-data | Не должен рассчитывать торговые сигналы |
| FeaturePipeline | Индикаторы и enrichment quality | Не должен принимать решения о сигнале |
| StrategyEngine | Запуск production/beta стратегий по manifest | Не должен скрывать data-quality failures |
| FilterEngine | Hard/soft gates и reject reasons | Не должен мутировать исходные market-data |
| Notifier | Доставка аналитических сигналов | Не должен иметь Binance credentials или trading rights |

## 13. Итоговая оценка готовности

Проект **ещё не выглядит готовым к безнадзорному production-запуску**, но уже имеет хорошую основу для signal-only аналитического Binance-бота. Самые сильные стороны — явная public-only политика, разделение WebSocket streams, REST allowlist, fallback shortlist и попытка учитывать funding/OI/long-short/basis данные. Самые слабые стороны — отсутствие доказанной Windows 11/Python 3.13 CI-матрицы, смешанные зависимости, перегруженный feature pipeline, экспериментальные roadmap-стратегии и недостаточная формализация качества данных.

Если исправить P0/P1 пункты, проект можно будет перевести из состояния “активная ИИ-разработка” в состояние “контролируемый signal-only runtime”. До этого любые новые стратегии или фильтры лучше добавлять только после того, как появятся public-boundary tests, live-smoke Binance tests, dependency profiles и строгие contracts данных.

## 14. Короткий чек-лист перед следующим коммитом

| Проверка | Должно быть |
|---|---|
| Binance keys | Не нужны и не используются |
| REST endpoints | Только allowlist public market-data |
| WS endpoints | Только `public` и `market`, без `private` |
| Orders/account | Любой код/URL/path с `order`, `account`, `positionRisk`, `listenKey` должен падать в tests |
| Python | Только `>=3.13,<3.14` в production, CI это проверяет |
| Windows | `windows-latest` в GitHub Actions |
| Dependencies | Минимальный runtime отдельно от ML/dev/live |
| Strategies | Experimental выключены по умолчанию |
| Filters | Все rejection reasons пишутся в telemetry |
| Data quality | Каждый сигнал содержит возраст данных и качество enrichment |

[^binance-ws]: Binance Developers, “USDⓈ-M Futures WebSocket Market Streams,” https://developers.binance.com/docs/derivatives/usds-margined-futures/websocket-market-streams.
[^binance-ws-change]: Binance Developers, “Important WebSocket Change Notice,” https://developers.binance.com/docs/derivatives/usds-margined-futures/websocket-market-streams/Important-WebSocket-Change-Notice.
[^binance-rest]: Binance Developers, “USDⓈ-M Futures Market Data REST API,” https://developers.binance.com/docs/derivatives/usds-margined-futures/market-data/rest-api.
[^binance-long-short]: Binance Developers, “Long/Short Ratio,” https://developers.binance.com/docs/derivatives/usds-margined-futures/market-data/rest-api/Long-Short-Ratio.
