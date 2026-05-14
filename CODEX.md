# \---

# name: Audit bot fixes

# overview: План объединяет форензику, сверку внешнего аудита (§0.7), наблюдения с дашборда по маршрутизации стратегий (§0.8), охват 37 сетапов/фильтров/Binance I/O, затем исправления движка/шортлиста, дашборда и Telegram.

# todos:

# &#x20; - id: diag-paths-correlate

# &#x20;   content: "Найти активный процесс: data/bot/bot.pid; последний bot\_\*.log в data/bot/logs; run-папку data/bot/telemetry/runs/<run\_id>/ с run\_metadata.json"

# &#x20;   status: pending

# &#x20; - id: diag-logs-grep

# &#x20;   content: "По лог-файлу сессии — grep по каталогу строк (см. раздел «Каталог grep»): strategy\_fits, telegram, delivery, ws, prepare, circuit"

# &#x20;   status: pending

# &#x20; - id: diag-jsonl-polars

# &#x20;   content: "Анализ analysis/\*.jsonl (candidates/rejected/selected, strategy\_decisions если есть) — частоты stage/reason, последние N строк"

# &#x20;   status: pending

# &#x20; - id: diag-sqlite-health

# &#x20;   content: "SQLite data/bot/bot.db + health.jsonl/health\_runtime.jsonl — активные сигналы, cooldown, ошибки цикла"

# &#x20;   status: pending

# &#x20; - id: diag-dashboard-http

# &#x20;   content: "HTTP к дашборду (host/port из config), сравнение с BOT\_DISABLE\_HTTP\_SERVERS и логами fastapi/uvicorn"

# &#x20;   status: pending

# &#x20; - id: deep-audit-strategies

# &#x20;   content: "Матрица 37 стратегий: setup\_id, файл, ключи filters.setups, вызовы setups/helpers; выборка чтения по 2–3 эталона + roadmap.py"

# &#x20;   status: pending

# &#x20; - id: deep-audit-filters-features

# &#x20;   content: "filters.py + symbol\_analyzer (воронка) + confluence/ml; features.prepare\_symbol и min\_required\_bars vs TF"

# &#x20;   status: pending

# &#x20; - id: deep-audit-shortlist-universe

# &#x20;   content: "universe.py (build\_shortlist, \_strategy\_fits\_for\_row, rerank) + shortlist\_service + oi\_refresh\_runner"

# &#x20;   status: pending

# &#x20; - id: deep-audit-binance-io

# &#x20;   content: "market\_data.py (REST веса, кэши, klines/OI/funding) + ws\_manager + websocket/\*; граница только public"

# &#x20;   status: pending

# &#x20; - id: reconcile-external-audit

# &#x20;   content: "Пройти таблицу внешнего аудита: подтвердить/опровергнуть каждый пункт в коде; зафиксировать ложные срабатывания (см. § ниже)"

# &#x20;   status: pending

# &#x20; - id: routing-vs-dashboard-evidence

# &#x20;   content: "§0.8: устранить доминирование asset\_fit.shortlist\_not\_routed — политика маршрутизации (engine+universe) + сверка с дашбордом после деплоя"

# &#x20;   status: pending

# &#x20; - id: strategy-quality-after-routing

# &#x20;   content: "После охвата маршрутизации: точечный разбор сетапов с отрицательным expectancy (wick\_trap, spread\_strategy, …), filters.setups, опционально default\_off в config"

# &#x20;   status: pending

# &#x20; - id: fix-engine-fits

# &#x20;   content: "После подтверждения гипотезы — вариант A или B для пустых strategy\_fits; engine.py/universe.py; pytest"

# &#x20;   status: pending

# &#x20; - id: fix-dashboard-js

# &#x20;   content: "Null-safe JS, fmt.score/pct, btc\_bias в API+UI; pytest dashboard"

# &#x20;   status: pending

# &#x20; - id: telegram-preflight-telemetry

# &#x20;   content: "preflight в start; WARNING при status!=sent; опционально metrics"

# &#x20;   status: pending

# &#x20; - id: arch-followups

# &#x20;   content: "Единый Telegram boundary, карта TF/freshness, rerank strategy\_fits"

# &#x20;   status: pending

# isProject: false

# \---

# 

# \# План аудита и исправлений (архитектура, логика, дашборд, Telegram)

# 

# \## Важно про «бот в отдельном окне»

# 

# Инструменты агента в Cursor видят только файловую систему \*\*рабочей папки проекта\*\* (`bot2`). Если бот запущен из \*\*той же\*\* директории, что и репозиторий, все артефакты ниже появляются на диске и их можно читать напрямую. Если рабочая папка другая — скопируйте в проект последний `bot\_\*.log` и папку `telemetry/runs/<run\_id>` (или укажите путь в следующем сообщении).

# 

# Корреляция сессии (один запуск `python main.py`):

# 

# | Артефакт | Путь (дефолты из \[`BotSettings`](c:/Users/undea/Documents/bot2/bot/domain/config.py)) |

# |----------|----------------------------------------------------------------------------------------|

# | PID | \[`data/bot/bot.pid`](c:/Users/undea/Documents/bot2/bot/domain/config.py) (см. свойство `pid\_file`) |

# | Лог сессии | `data/bot/logs/bot\_<YYYYMMDD\_HHMMSS>\_<pid>.log` — создаётся в \[`configure\_logging`](c:/Users/undea/Documents/bot2/bot/cli.py); в начале файла есть строка `LOG FILE \\| ...` |

# | Телеметрия run | `data/bot/telemetry/runs/<run\_id>/` где `run\_id = <UTC Ymd\_HMS>\_<pid>` — задаётся в \[`\_main`](c:/Users/undea/Documents/bot2/bot/cli.py) и \[`TelemetryStore`](c:/Users/undea/Documents/bot2/bot/telemetry.py) |

# | Parquet / DB | `data/bot/bot.db`, `data/bot/parquet/` (через `MemoryRepository`) |

# 

# Поддиректория телеметрии задаётся `runtime.telemetry\_subdir` (в примере \[`config.toml.example`](c:/Users/undea/Documents/bot2/config.toml.example): `telemetry`).

# 

# \---

# 

# \## Фаза 0 (расширенная): форензика логов и телеметрии до любых правок кода

# 

# \### 0.1 Зафиксировать «какую сессию» смотрим

# 

# 1\. Прочитать `data/bot/bot.pid` — сравнить с PID процесса в отдельном окне (Диспетчер задач / `Get-Process python`).

# 2\. В `data/bot/logs/` взять \*\*самый новый по времени изменения\*\* `bot\_\*.log` с тем же PID в суффиксе имени (или строку `LOG FILE` из консоли того окна).

# 3\. Найти каталог `data/bot/telemetry/runs/` — выбрать каталог с тем же `run\_id` (совпадает с PID в хвосте после `\_`).

# 

# \### 0.2 Каталог grep по \*\*лог-файлу\*\* (разделение гипотез)

# 

# Искать подстроки (регистр как в логгере; часть сообщений идёт через structlog и может быть JSON — тогда `grep` + глазами или `rg` по ключам).

# 

# \*\*Пайплайн «нет сигналов до Telegram» (анализ не запускается / шортлист):\*\*

# 

# \- `calculate\_all skipped | no strategy\_fits` — \[`SignalEngine`](c:/Users/undea/Documents/bot2/bot/core/engine/engine.py): символ на шортлисте с `shortlist\_score`, но пустые `strategy\_fits` и не deep-analysis.

# \- `calculate\_all called | strategies=` — анализ реально стартовал; смотреть дальше по стратегиям.

# \- `skipped - insufficient data` — нет свечей/контекста для TF.

# \- `Strategy ... timed out` — таймаут стратегии.

# \- `shortlist` / `shortlist build` / `pinned` / `ws\_manager` — см. логи \[`ShortlistService`](c:/Users/undea/Documents/bot2/bot/application/shortlist\_service.py), \[`SignalBot.start`](c:/Users/undea/Documents/bot2/bot/application/bot.py).

# 

# \*\*Пайплайн «кандидаты есть, Telegram нет»:\*\*

# 

# \- `local signal logged` — \[`SignalDelivery.deliver`](c:/Users/undea/Documents/bot2/bot/delivery.py): `DisabledBroadcaster` / `notifier\_disabled` / не Telegram.

# \- `telegram message sent` / structlog `telegram message sent` — успешный вывод в чат.

# \- `telegram preflight failed` — только если вызывали preflight из messaging (сейчас на старте бота \*\*не\*\* вызывается — но может всплыть вручную).

# \- `suppressing duplicate telegram`, `circuit breaker`, `telegram send failed`, `rate limited` — \[`TelegramBroadcaster`](c:/Users/undea/Documents/bot2/bot/messaging.py).

# 

# \*\*Дашборд / HTTP:\*\*

# 

# \- `dashboard server disabled`, `failed to import uvicorn`, `dashboard server crashed` — \[`BotDashboard.start\_server`](c:/Users/undea/Documents/bot2/bot/dashboard.py).

# \- `http servers disabled via BOT\_DISABLE\_HTTP\_SERVERS` — \[`SignalBot.\_\_init\_\_`](c:/Users/undea/Documents/bot2/bot/application/bot.py).

# 

# \*\*Здоровье цикла:\*\*

# 

# \- `STDERR:` (перехват stderr в CLI) — неочевидные падения библиотек.

# \- `Unhandled exception` — handler в \[`\_main`](c:/Users/undea/Documents/bot2/bot/cli.py).

# 

# \### 0.3 JSONL в `telemetry/runs/<run\_id>/analysis/` (количественно)

# 

# \[`TelemetryStore.append\_jsonl`](c:/Users/undea/Documents/bot2/bot/telemetry.py) пишет в \*\*`analysis/`\*\* под текущим run. Писатели (неполный список, достаточный для диагностики «почему тихо»):

# 

# | Файл | Кто пишет | Вопрос, на который отвечает |

# |------|-----------|------------------------------|

# | `candidates.jsonl` | \[`CycleRunner`](c:/Users/undea/Documents/bot2/bot/application/cycle\_runner.py) | Были ли кандидаты сигналов после анализа |

# | `selected.jsonl` | тот же | Что прошло отбор и ушло в доставку (лог строки сигнала) |

# | `rejected.jsonl` | тот же + др. | На каком \*\*stage\*\* отрезало (memory / tracking / cooldown / фильтры — см. поля в строках) |

# | `rejections.jsonl` | сводка по циклу | Агрегированные счётчики причин |

# | `shortlist.jsonl` | \[`ShortlistService`](c:/Users/undea/Documents/bot2/bot/application/shortlist\_service.py) | Обновления шортлиста, fallback-причины |

# | `health.jsonl` / соседние | \[`HealthManager`](c:/Users/undea/Documents/bot2/bot/application/health\_manager.py) | Снимки здоровья |

# | `strategy\_decisions.jsonl` | (если включено в пайплайне) | Воронка по стратегиям; дашборд читает через glob в \[`dashboard.\_latest\_analysis\_files`](c:/Users/undea/Documents/bot2/bot/dashboard.py) |

# 

# \*\*Практика анализа (без обязательного кода):\*\* за последние 200–500 строк `candidates.jsonl` и `rejected.jsonl` — сгруппировать по полю `reason` или `stage` (вручную или одним скриптом на Polars в `scripts/` — только если решите автоматизировать).

# 

# \*\*Замечание по дашборду:\*\* блок «Recent activity» берёт `candidates.jsonl` через \[`\_latest\_analysis\_file`](c:/Users/undea/Documents/bot2/bot/dashboard.py) из `telemetry/runs/\*/analysis/candidates.jsonl`. Если видите данные в JSONL, но пусто в UI — смотреть консоль браузера (JS) и ответы `/api/signals/recent`.

# 

# \### 0.4 SQLite `data/bot/bot.db`

# 

# Имеет смысл сравнить с CLI `python -m bot.cli status` (\[`\_run\_status\_command`](c:/Users/undea/Documents/bot2/bot/cli.py)): активные сигналы, outcomes. Если в логе есть `selected`, а в Telegram пусто — смотреть, дошёл ли `deliver` до `sent` и сработал ли `arm\_signals\_with\_messages` (логи tracking/delivery).

# 

# \### 0.5 HTTP дашборда

# 

# Запросы к `http://<dashboard\_host>:<dashboard\_port>/api/status` и `/api/health` (значения из `config.toml`). Сверить с логом: не отключён ли сервер (`BOT\_DISABLE\_HTTP\_SERVERS`), поднялся ли uvicorn.

# 

# \### 0.6 Глубокий аудит кода (то, что ранее не было разложено по файлам)

# 

# Здесь — \*\*обязательный охват перед выводами\*\* о торговой логике и таймфреймах. Читать не «от корки до корки» каждый огромный модуль, а по контрактам из \[`bot/strategies/AGENTS.md`](c:/Users/undea/Documents/bot2/bot/strategies/AGENTS.md) и цепочке вызовов: `prepare\_symbol` → `PreparedSymbol` → `SignalEngine` → фильтры/конфлюэнс → доставка.

# 

# \#### A. Все стратегии (37 классов в `STRATEGY\_CLASSES`)

# 

# Источник истины: \[`bot/strategies/\_\_init\_\_.py`](c:/Users/undea/Documents/bot2/bot/strategies/\_\_init\_\_.py) (`STRATEGY\_CLASSES`).

# 

# | Файл | Классы (setup\_id = атрибут класса, проверять в коде) |

# |------|------------------------------------------------------|

# | \[`structure\_pullback.py`](c:/Users/undea/Documents/bot2/bot/strategies/structure\_pullback.py) | StructurePullbackSetup |

# | \[`structure\_break\_retest.py`](c:/Users/undea/Documents/bot2/bot/strategies/structure\_break\_retest.py) | StructureBreakRetestSetup |

# | \[`wick\_trap\_reversal.py`](c:/Users/undea/Documents/bot2/bot/strategies/wick\_trap\_reversal.py) | WickTrapReversalSetup |

# | \[`squeeze\_setup.py`](c:/Users/undea/Documents/bot2/bot/strategies/squeeze\_setup.py) | SqueezeSetup |

# | \[`ema\_bounce.py`](c:/Users/undea/Documents/bot2/bot/strategies/ema\_bounce.py) | EmaBounceSetup |

# | \[`fvg.py`](c:/Users/undea/Documents/bot2/bot/strategies/fvg.py) | FVGSetup |

# | \[`order\_block.py`](c:/Users/undea/Documents/bot2/bot/strategies/order\_block.py) | OrderBlockSetup |

# | \[`liquidity\_sweep.py`](c:/Users/undea/Documents/bot2/bot/strategies/liquidity\_sweep.py) | LiquiditySweepSetup |

# | \[`bos\_choch.py`](c:/Users/undea/Documents/bot2/bot/strategies/bos\_choch.py) | BOSCHOCHSetup |

# | \[`hidden\_divergence.py`](c:/Users/undea/Documents/bot2/bot/strategies/hidden\_divergence.py) | HiddenDivergenceSetup |

# | \[`funding\_reversal.py`](c:/Users/undea/Documents/bot2/bot/strategies/funding\_reversal.py) | FundingReversalSetup |

# | \[`cvd\_divergence.py`](c:/Users/undea/Documents/bot2/bot/strategies/cvd\_divergence.py) | CVDDivergenceSetup |

# | \[`session\_killzone.py`](c:/Users/undea/Documents/bot2/bot/strategies/session\_killzone.py) | SessionKillzoneSetup |

# | \[`breaker\_block.py`](c:/Users/undea/Documents/bot2/bot/strategies/breaker\_block.py) | BreakerBlockSetup |

# | \[`turtle\_soup.py`](c:/Users/undea/Documents/bot2/bot/strategies/turtle\_soup.py) | TurtleSoupSetup |

# | \[`vwap\_trend.py`](c:/Users/undea/Documents/bot2/bot/strategies/vwap\_trend.py) | VWAPTrendSetup |

# | \[`supertrend\_follow.py`](c:/Users/undea/Documents/bot2/bot/strategies/supertrend\_follow.py) | SuperTrendFollowSetup |

# | \[`price\_velocity.py`](c:/Users/undea/Documents/bot2/bot/strategies/price\_velocity.py) | PriceVelocitySetup |

# | \[`volume\_anomaly.py`](c:/Users/undea/Documents/bot2/bot/strategies/volume\_anomaly.py) | VolumeAnomalySetup |

# | \[`volume\_climax\_reversal.py`](c:/Users/undea/Documents/bot2/bot/strategies/volume\_climax\_reversal.py) | VolumeClimaxReversalSetup |

# | \[`keltner\_breakout.py`](c:/Users/undea/Documents/bot2/bot/strategies/keltner\_breakout.py) | KeltnerBreakoutSetup |

# | \[`roadmap.py`](c:/Users/undea/Documents/bot2/bot/strategies/roadmap.py) | AbsorptionSetup, AggressionShiftSetup, AltcoinSeasonIndexSetup, ATRExpansionSetup, BBSqueezeSetup, BTCCorrelationSetup, DepthImbalanceSetup, LiquidationHeatmapSetup, LSRatioExtremeSetup, MultiTFTrendSetup, OIDivergenceSetup, RSIDivergenceBottomSetup, SpreadStrategySetup, StopHuntDetectionSetup, WhaleWallsSetup, WyckoffSpringSetup |

# 

# \*\*Чеклист на каждую стратегию (коротко, но явно):\*\* наследование от \[`setup\_base.BaseSetup`](c:/Users/undea/Documents/bot2/bot/setup\_base.py); стабильный `setup\_id`; `detect(prepared, settings)`; используемые колонки/TF в `prepared` (15m/1h/4h/5m); пороги из `settings.filters.setups\[setup\_id]` vs захардкоженные константы; согласованность сигнального поля с \[`bot/domain/schemas.py`](c:/Users/undea/Documents/bot2/bot/domain/schemas.py) / legacy \[`bot/models.py`](c:/Users/undea/Documents/bot2/bot/models.py) (что реально строит движок — сверить импорты в engine).

# 

# \*\*Эффективный порядок чтения:\*\* 2–3 «эталонных» сетапа из разных семейств (структура / объём / funding) + целиком \[`roadmap.py`](c:/Users/undea/Documents/bot2/bot/strategies/roadmap.py) как пакет однотипных детекторов.

# 

# \#### B. Общие хелперы сетапов (blast radius)

# 

# \- \[`bot/setups.py`](c:/Users/undea/Documents/bot2/bot/setups.py), каталог \[`bot/setups/`](c:/Users/undea/Documents/bot2/bot/setups/), \[`bot/setups/utils.py`](c:/Users/undea/Documents/bot2/bot/setups/utils.py) — общие уровни, RR, свечные паттерны.

# \- Любое изменение здесь требует перекрёстной проверки нескольких стратегий из таблицы выше.

# 

# \#### C. Фильтры и пост-обработка сигнала

# 

# \- Контракт порогов: \[`FilterConfig`](c:/Users/undea/Documents/bot2/bot/domain/config.py) + `filters.setups` (per-setup overrides).

# \- Реализация гейтов: \[`bot/filters.py`](c:/Users/undea/Documents/bot2/bot/filters.py) — свежесть свечей (`freshness\_\*`), спред, ATR%, mark deviation, min RR/score, ADX override из `setups`.

# \- Где вызывается в рантайме: \[`bot/application/symbol\_analyzer.py`](c:/Users/undea/Documents/bot2/bot/application/symbol\_analyzer.py) (подготовка, ошибки `prepare\_symbol`, воронка в telemetry).

# \- Конфлюэнс / ML: \[`bot/confluence.py`](c:/Users/undea/Documents/bot2/bot/confluence.py), \[`bot/ml/`](c:/Users/undea/Documents/bot2/bot/ml/) — отдельно зафиксировать, \*на каком этапе\* отбрасывается сигнал и что пишется в `strategy\_decisions` / лог.

# 

# \#### D. Шортлист и маршрутизация стратегий

# 

# \- Построение и скоринг: \[`bot/universe.py`](c:/Users/undea/Documents/bot2/bot/universe.py) — `build\_shortlist`, `\_strategy\_fits\_for\_row`, бакеты, pinned, `\_ALL\_SETUP\_IDS` в \[`config.py`](c:/Users/undea/Documents/bot2/bot/domain/config.py).

# \- Жизненный цикл в боте: \[`bot/application/shortlist\_service.py`](c:/Users/undea/Documents/bot2/bot/application/shortlist\_service.py), обновление OI: \[`bot/application/oi\_refresh\_runner.py`](c:/Users/undea/Documents/bot2/bot/application/oi\_refresh\_runner.py).

# \- Связка с движком: \[`bot/core/engine/engine.py`](c:/Users/undea/Documents/bot2/bot/core/engine/engine.py) (`strategy\_fits`, deep\_analysis, routing skips).

# 

# \#### E. Получение и обработка рыночных данных (Binance public only)

# 

# \- REST-клиент и кэши TTL: \[`bot/market\_data.py`](c:/Users/undea/Documents/bot2/bot/market\_data.py) — база `\_FAPI\_BASE\_URL`, лимиты веса (`\_REST\_WEIGHT\_\*`, `\_ENDPOINT\_WEIGHTS`), семафоры, `preflight\_check`, klines/mark/funding/OI и т.д. (файл большой — по оглавлению методов и `rg`, не построчно).

# \- WebSocket: \[`bot/ws\_manager.py`](c:/Users/undea/Documents/bot2/bot/ws\_manager.py), \[`bot/websocket/`](c:/Users/undea/Documents/bot2/bot/websocket/) — подписки, `rest\_full` vs `ws\_light`, fallback (см. правила репозитория про явную телеметрию).

# \- Сборка признаков: \[`bot/features.py`](c:/Users/undea/Documents/bot2/bot/features.py) — функция \[`prepare\_symbol`](c:/Users/undea/Documents/bot2/bot/features.py) и требования к числу баров (`min\_required\_bars` / настройки `filters.min\_bars\_\*`), согласованность TF с \[`runtime\_policy.py`](c:/Users/undea/Documents/bot2/bot/runtime\_policy.py).

# 

# \#### F. Критерий завершения «глубокого» слоя

# 

# По каждому из блоков A–E заполнена краткая таблица: \*\*входные данные (TF/источник)\*\* → \*\*ключевые пороги (config vs код)\*\* → \*\*известные режимы отказа\*\* → \*\*ссылка на тесты\*\* (`rg` по `tests/test\_\*` для затронутых модулей).

# 

# \### 0.7 Внешний аудит (входной отчёт) — сверка с текущим репозиторием

# 

# Ниже — интеграция вашего структурированного отчёта с \*\*проверкой по актуальному коду\*\* (май 2026, дерево `bot2`). Перед реализацией любого пункта повторить `rg`/чтение: отчёт мог быть составлен по другой ревизии.

# 

# \*\*Легенда:\*\* подтверждено в коде | опровергнуто / устарело | частично / семантика | осталось проверить при исполнении

# 

# | ID | Тема | Статус | Сверка |

# |----|------|--------|--------|

# | 1 | `threading.RLock` в `\_FrameCache` при async | подтверждено | \[`features.py`](c:/Users/undea/Documents/bot2/bot/features.py): класс `\_FrameCache`, `self.\_lock = threading.RLock()`. Риск блокировки event loop при конкурентных `prepare\_symbol` — остаётся в плане как P0/P1. |

# | 2 | Версии в `requirements.txt` / `pyproject.toml` | подтверждено как риск | Файл \[`requirements.txt`](c:/Users/undea/Documents/bot2/requirements.txt) действительно задаёт агрессивные нижние границы (`pytest>=9.0.3`, `fastapi>=0.136.1`, и т.д.). \*\*Версии на PyPI\*\* нужно подтвердить командой `pip install -r requirements.txt` (или `uv pip compile`) на целевом Python — таблица «последняя \~8.x» в отчёте может устареть; не копировать как факт без прогона. |

# | 3 | EMA: `ewm\_mean(span=N)` якобы даёт α=1/N | \*\*опровергнуто\*\* для Polars | Официальная документация Polars: при `span=θ` используется \*\*α = 2/(θ+1)\*\* (\[`polars.Expr.ewm\_mean`](https://docs.pola.rs/api/python/stable/reference/expressions/api/polars.Expr.ewm\_mean.html)), т.е. совпадает со стандартной EMA. Fallback в \[`\_ema`](c:/Users/undea/Documents/bot2/bot/features.py) с `span=period` \*\*корректен\*\* для Polars; отдельно проверить ветку \*\*polars\_ta\*\* `plta.EMA` на соответствие, если бэкенд включён. |

# | 4 | Дубли `\_call\_rest` / `\_call\_public\_http\_json` | подтверждено на уровне структуры | В \[`market\_data.py`](c:/Users/undea/Documents/bot2/bot/market\_data.py) оба метода существуют (\~698 и \~813); рекомендация объединить — архитектурный P1. |

# | 5 | `ZeroDivisionError` в `entry\_reference\_price` | \*\*опровергнуто\*\* | \[`schemas.py`](c:/Users/undea/Documents/bot2/bot/domain/schemas.py): ветка с делением выполняется только при `mid > 0`. |

# | 6 | Freshness primary: всегда `work\_15m` | \*\*опровергнуто\*\* | \[`filters.py`](c:/Users/undea/Documents/bot2/bot/filters.py): `primary\_frame = \_frame\_for\_timeframe(prepared, primary\_timeframe)` затем `\_frame\_is\_fresh(primary\_frame, primary\_freshness)`. Отчётный фрагмент кода не совпадает с текущей версией. |

# | 7 | ADX: hard\_gate→penalty для deep без логов | частично | Код \[`filters.py`](c:/Users/undea/Documents/bot2/bot/filters.py) \~237–240 действительно молча меняет `adx\_policy`; в детали scoring передаётся `deep\_analysis\_asset` — явного \*\*log warning\*\* нет; при желании P2: один `LOG.info` при переключении политики. |

# | 8 | Strategy seed «съедает весь шортлист» | \*\*уточнено\*\* | В \[`universe.py`](c:/Users/undea/Documents/bot2/bot/universe.py) порядок: \*\*сначала\*\* бакеты trend/breakout/reversal, \*\*затем\*\* резерв по `\_ALL\_SETUP\_IDS` (37×до 2), \*\*затем\*\* `fill`. Резерв не отбирает слоты у бакетов, но может \*\*сильно съесть фазу `fill`\*\* при малом `shortlist\_limit` — формулировка в отчёте про бакеты неточна. |

# | 9–10, 23–24 | Логика roadmap (RSI, StopHunt/Wyckoff, BB, OI) | осталось проверить при исполнении | Читать \[`roadmap.py`](c:/Users/undea/Documents/bot2/bot/strategies/roadmap.py) по классам из отчёта; решения: переименование, общий хелпер, комментарии к OI/price. |

# | 11 | `TCPConnector(limit=5)` | \*\*опровергнуто\*\* | \[`market\_data.py`](c:/Users/undea/Documents/bot2/bot/market\_data.py): `TCPConnector(limit=\_HTTP\_CONNECTOR\_LIMIT)`; константа в начале файла \*\*50\*\* (`\_HTTP\_CONNECTOR\_LIMIT`). |

# | 12 | Неверный case пути taker | \*\*опровергнуто для URL\*\* | Реестр \[`\_PUBLIC\_ENDPOINT\_REGISTRY`](c:/Users/undea/Documents/bot2/bot/market\_data.py) использует `/futures/data/takerLongShortRatio`. В docstring `fetch\_taker\_ratio` — опечатка `takerlongshortRatio` (косметика). |

# | 13 | Gap backfill без лимита задач | \*\*опровергнуто / смягчено\*\* | \[`ws\_manager.py`](c:/Users/undea/Documents/bot2/bot/ws\_manager.py): при gap вызывается `\_schedule\_backfill`, есть `\_backfill\_sem`, множество `\_backfill\_tasks`, `\_backfill\_symbols\_inflight`. Не сырой `create\_task(self.\_backfill)` как в цитате отчёта. |

# | 14 | `await asyncio.sleep(0)` в hot path | осталось оценить профилем | Имеет смысл только при профилировании на реальном потоке `!ticker@arr`. |

# | 15 | Python-циклы в индикаторах | подтверждено намерением проверки | Нужно точечно `rg` по `\_supertrend|\_parabolic\_sar|\_fisher` в \[`features.py`](c:/Users/undea/Documents/bot2/bot/features.py). |

# | 16–17 | `time.time()` vs монотонность / clock skew | подтверждено для части путей | Например \[`ws\_manager.py`](c:/Users/undea/Documents/bot2/bot/ws\_manager.py) `\_should\_drop\_stale\_event`: `age\_ms = (time.time() \* 1000.0) - event\_ms` — замечание отчёта \*\*валидно\*\* как P2/P3. |

# | 19 | `has\_minimum\_bars` не проверяет 4h/5m | \*\*опровергнуто\*\* | Текущая реализация \[`has\_minimum\_bars`](c:/Users/undea/Documents/bot2/bot/features.py) итерирует все TF из словаря с `required` из `minimums`. |

# | 20 | `rerank\_shortlist` не обновляет score/fits | подтверждено | Как в плане ранее — \[`universe.py`](c:/Users/undea/Documents/bot2/bot/universe.py) `rerank\_shortlist`. |

# | 30 | Realized vol «annualization» | частично | В коде \[`\_realized\_volatility`](c:/Users/undea/Documents/bot2/bot/features.py) используется `rolling\_std \* sqrt(period) \* 100` — это \*\*не\*\* годовая волатильность в классическом смысле; вопрос именования/интерпретации и документации колонки, а не обязательно «ошибка ×35» без уточнения единиц баров. |

# 

# \*\*Итог для фазы исполнения:\*\* объединить подтверждённые пункты (1, 2, 4, 7-уточнение, 8-уточнение, 15–17, 20, roadmap 9–10/23–24) с уже запланированными фиксами движка/дашборда/Telegram; \*\*не\*\* переносить в P0 опровергнутые пункты 3, 5, 6, 11, 12 (URL), 13, 19 без повторной проверки.

# 

# \### 0.8 Наблюдения с дашборда (live): «стратегии не работают» vs реальная причина

# 

# Переданные цифры (Performance 30d, Strategy Performance, Strategy Decision Diagnostics) \*\*согласуются с архитектурой маршрутизации\*\*, а не только с флагами `\[bot.setups]` в `config.toml`.

# 

# \*\*Ключевая строка в диагностике:\*\* доминирует причина \*\*`asset\_fit.shortlist\_not\_routed`\*\* (часто с одинаковым знаменателем \~5406 строк — это объём телеметрии решений за окно, не «всего сигналов»). В коде это означает: для данного символа/цикла \*\*`UniverseSymbol.strategy\_fits` не содержит `setup\_id`\*\*, и движок (\[`SignalEngine.calculate\_all`](c:/Users/undea/Documents/bot2/bot/core/engine/engine.py)) либо \*\*не вызывает\*\* детектор вовсе (ветка routing / `emit\_strategy\_routing\_skips`), либо пишет skip с этой причиной. То есть сетап может быть \*\*включён в конфиге\*\*, но \*\*не попадает в расчёт\*\* на большинстве тиков шортлиста.

# 

# \*\*Блок «0 / 0.0% / 0.00R» в Strategy Performance\*\* (\[`StrategyAnalytics`](c:/Users/undea/Documents/bot2/bot/analytics.py) + `get\_setup\_stats` / outcomes) чаще означает \*\*нет закрытых сделок/outcomes за 30d по этому `setup\_id`\*\*, а не обязательно «детектор ни разу не сработал». Отличать: (а) нет маршрутизации → мало `signal` в decisions; (б) есть маршрутизация, но `pattern.\*` / фильтры → мало кандидатов; (в) есть сигналы, но нет исходов в SQLite.

# 

# \*\*Блок с низким win rate / отрицательным expectancy\*\* (например `wick\_trap\_reversal` 0% / -0.67R, `spread\_strategy` -0.56R) — это уже \*\*качество сетапа и фильтров\*\*, а не «включить в конфиге». После исправления маршрутизации имеет смысл \*\*точечный аудит\*\* таких сетапов (\[`bot/strategies/`](c:/Users/undea/Documents/bot2/bot/strategies/), \[`filters.setups`](c:/Users/undea/Documents/bot2/bot/domain/config.py)), вплоть до \*\*выключения по умолчанию\*\* или ужесточения порогов, иначе «включить все» ухудшит PnL и шум в Telegram.

# 

# \*\*Зафиксированное решение владельца (маршрутизация):\*\* на символах \*\*шортлиста\*\* в рантайме считать \*\*все стратегии, включённые в `config` / registry\*\* (максимум охвата; \*\*рост CPU\*\* и объёма телеметрии ожидаем). Реализация — в первую очередь \[`SignalEngine.calculate\_all`](c:/Users/undea/Documents/bot2/bot/core/engine/engine.py): убрать/обойти сужение по `UniverseSymbol.strategy\_fits` для live shortlist (сохранить или перепрофилировать `strategy\_fits` как подсказку/телеметрию при необходимости). Это напрямую бьёт по доле \*\*`asset\_fit.shortlist\_not\_routed`\*\* в дашборде.

# 

# \*\*Планируемые направления работ (связать с `fix-engine-fits` и todo `routing-vs-dashboard-evidence`):\*\*

# 

# 1\. \*\*Маршрутизация / охват:\*\* реализовать решение выше + оставить вариант B (доработка `\_strategy\_fits\_for\_row` / pinned) как \*\*дополнительную\*\* оптимизацию или для режима «лёгкий CPU», если позже введёте флаг. После деплоя — снова смотреть долю `shortlist\_not\_routed` в `/api/analytics/strategy-decisions`.

# 2\. \*\*Качество:\*\* отдельный проход по сетапам с худшим `expectancy\_r` при достаточном `trades`; не смешивать с «включить все» без анализа.

# 3\. \*\*Прозрачность дашборда (опционально):\*\* подписать в UI разницу между «outcomes за 30d» и «decision rows за run», чтобы не путать нули.

# 

# \---

# 

# \## Подтверждённые риски в коде (не догадки)

# 

# \### 1. Торговая логика / движок: полный пропуск стратегий

# 

# В \[`bot/core/engine/engine.py`](c:/Users/undea/Documents/bot2/bot/core/engine/engine.py) после маршрутизации по `strategy\_fits` есть ветка: если \*\*`strategy\_fits` пуст\*\*, при этом у `prepared.universe` задан \*\*`shortlist\_score`\*\* и символ \*\*не\*\* помечен как `deep\_analysis`, метод \*\*`calculate\_all` возвращает пустой список\*\* — ни одна стратегия не вызывается.

# 

# В \[`bot/universe.py`](c:/Users/undea/Documents/bot2/bot/universe.py) \*\*закреплённые (pinned) символы\*\* попадают в шортлист \*\*без проверки\*\* `strategy\_fits`. \[`\_strategy\_fits\_for\_row`](c:/Users/undea/Documents/bot2/bot/universe.py) может вернуть \*\*пустой кортеж\*\*.

# 

# \*\*Итог:\*\* «тишина» в Telegram возможна \*\*без сетевой ошибки\*\* — подтверждается логом `calculate\_all skipped | no strategy\_fits` и/или пустым `candidates.jsonl` при ненулевом shortlist.

# 

# \*\*Направление исправления:\*\* по решению владельца (§0.8) — \*\*вариант A в расширенной форме:\*\* на шортлисте выполнять полный набор \*\*включённых\*\* стратегий (не только fallback при пустых `strategy\_fits`). Дополнительно возможен \*\*вариант B\*\* для pinned/узких fits или режима экономии CPU.

# 

# Сопутствующий техдолг: \[`rerank\_shortlist`](c:/Users/undea/Documents/bot2/bot/universe.py) не пересчитывает `strategy\_fits`.

# 

# \### 2. Дашборд: хрупкий клиентский JS

# 

# В \[`bot/dashboard.py`](c:/Users/undea/Documents/bot2/bot/dashboard.py): `setInterval` без null-check на `.nav-tab.active`; hotkeys без проверки; `fmt.score`/`fmt.pct` с falsy для `0`; `btc-bias` в HTML не заполняется из `/api/status`.

# 

# \### 3. Telegram: «тихие» статусы

# 

# \[`DisabledBroadcaster`](c:/Users/undea/Documents/bot2/bot/messaging.py) → статус \*\*`logged`\*\* в \[`SignalDelivery.deliver`](c:/Users/undea/Documents/bot2/bot/delivery.py). Нет \*\*`preflight\_check`\*\* в \[`SignalBot.start`](c:/Users/undea/Documents/bot2/bot/application/bot.py). Дубликаты/circuit в \[`TelegramBroadcaster.send\_html`](c:/Users/undea/Documents/bot2/bot/messaging.py).

# 

# Дублирование стеков: `messaging` vs `bot/telegram` — runtime использует \[`build\_application\_container`](c:/Users/undea/Documents/bot2/bot/application/container.py).

# 

# \---

# 

# \## Архитектура (кратко)

# 

# \- Цепочка: `SignalBot` → EventBus / циклы → `SignalEngine` → отбор/фильтры → `DeliveryOrchestrator` → `SignalDelivery` → `MessageBroadcaster`; персистентность \[`MemoryRepository`](c:/Users/undea/Documents/bot2/bot/core/memory/repository.py).

# \- TF / freshness / rest vs ws размазаны по \[`market\_data`](c:/Users/undea/Documents/bot2/bot/market\_data.py), \[`runtime\_policy`](c:/Users/undea/Documents/bot2/bot/runtime\_policy.py), \[`features`](c:/Users/undea/Documents/bot2/bot/features.py) — после форензики при необходимости добавить внутреннюю карту в план рефакторинга (Фаза 4).

# 

# ```mermaid

# flowchart LR

# &#x20; subgraph ingest \[MarketData]

# &#x20;   WS\[FuturesWSManager]

# &#x20;   REST\[BinanceFuturesMarketData]

# &#x20; end

# &#x20; subgraph core \[Analysis]

# &#x20;   Prep\[prepare\_symbol]

# &#x20;   Eng\[SignalEngine]

# &#x20;   Fil\[Filters confluence ML]

# &#x20; end

# &#x20; subgraph artifacts \[Telemetry]

# &#x20;   J\[candidates rejected selected jsonl]

# &#x20;   H\[health jsonl]

# &#x20; end

# &#x20; subgraph out \[Output]

# &#x20;   Del\[DeliveryOrchestrator]

# &#x20;   TG\[MessageBroadcaster]

# &#x20;   Dash\[Dashboard FastAPI]

# &#x20; end

# &#x20; WS --> Prep

# &#x20; REST --> Prep

# &#x20; Prep --> Eng

# &#x20; Eng --> Fil

# &#x20; Fil --> Del

# &#x20; Del --> TG

# &#x20; Eng --> J

# &#x20; Del --> J

# &#x20; SignalBot --> Dash

# ```

# 

# \---

# 

# \## Фазы работ после форензики

# 

# \### Фаза 1 — Движок / шортлист

# 

# По результатам 0.2–0.3: если подтверждён skip по `strategy\_fits` — реализовать выбранный вариант A/B; узкий pytest.

# 

# \### Фаза 2 — Дашборд

# 

# JS hardening + поля API; \[`tests/test\_dashboard\_security.py`](c:/Users/undea/Documents/bot2/tests/test\_dashboard\_security.py).

# 

# \### Фаза 3 — Telegram и наблюдаемость

# 

# `preflight\_check` в `start`; WARNING при `status != sent`; опционально metrics.

# 

# \### Фаза 4 — Архитектурные follow-up

# 

# Единый Telegram boundary; карта TF; rerank `strategy\_fits`.

# 

# \---

# 

# \## Критерий «форензика завершена»

# 

# Можно переходить к коду, когда по одной сессии заполнено:

# 

# 1\. Идентифицированы `bot\_\*.log` и `telemetry/runs/<run\_id>/`.

# 2\. Зафиксировано: есть ли строки в `candidates.jsonl` / `selected.jsonl` за последние часы.

# 3\. Зафиксировано: есть ли в логе `calculate\_all skipped | no strategy\_fits` или ошибки Telegram/circuit/notifier\_disabled.

# 4\. Проверен HTTP дашборда или причина его отсутствия (env, uvicorn, порт).

# 5\. Выполнен раздел \*\*0.6\*\* (таблица A–E + критерий F): стратегии, фильтры, шортлист, Binance I/O, `prepare\_symbol` — не обязательно полное построчное чтение `market\_data.py`/`features.py`, но явная карта контрактов и тестовых ссылок.

# 

# До выполнения пунктов 1–4 правки по симптомам «с нуля» преждевременны; пункт 5 обязателен перед архитектурными выводами о «каждой стратегии и каждом TF».



