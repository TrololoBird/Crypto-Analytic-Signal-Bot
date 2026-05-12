# Scripts inventory

Ниже зафиксирован актуальный реестр операторских/диагностических скриптов из `scripts/`.

| script | назначение | статус (active/deprecated) | входные параметры | пример запуска |
|---|---|---|---|---|
| `check_scripts_readme.py` | CI-guard: проверяет, что каждый `scripts/*.py` описан в этой таблице. | active | нет | `python scripts/check_scripts_readme.py` |
| `common.py` | Общие утилиты для CLI-скриптов (например, нормализация `sys.path` до корня репозитория). | active | нет (модуль-утилита) | `python -c "from scripts.common import bootstrap_repo_path"` |
| `live_check_binance_api.py` | Live smoke-check REST/WS Binance (коннект, реконы, свежесть потоков). | active | `--symbols`, `--warmup-seconds`, `--reconnect-wait-seconds` | `python -m scripts.live_check_binance_api --symbols BTCUSDT ETHUSDT` |
| `live_check_enrichments.py` | Live-проверка заполненности enrichment-полей в `PreparedSymbol`. | active | `--symbols`, `--warmup`, `--show-values` | `python scripts/live_check_enrichments.py --symbols BTCUSDT ETHUSDT --warmup 30` |
| `live_check_indicators.py` | Live-проверка индикаторов/подготовки фреймов по символам или run-id. | active | `--symbols`, `--run-id`, `--concurrency` | `python scripts/live_check_indicators.py --symbols BTCUSDT ETHUSDT` |
| `live_check_pipeline.py` | Live-проверка полной воронки кандидатов без отправки Telegram-сообщений. | active | `--symbols`, `--run-id`, `--concurrency`, `--warmup` | `python scripts/live_check_pipeline.py --symbols BTCUSDT ETHUSDT --warmup 30` |
| `live_check_strategies.py` | Live-проверка отработки стратегий и распределения решений. | active | `--symbols`, `--run-id`, `--concurrency` | `python scripts/live_check_strategies.py --symbols BTCUSDT ETHUSDT` |
| `live_runtime_monitor.py` | Live-мониторинг runtime-логов и сбор сводной статистики. | active | `--duration`, `--poll-interval`, `--log-dir` | `python -m scripts.live_runtime_monitor --duration 1800` |
| `live_smoke_bot.py` | Smoke-запуск `SignalBot` с fake broadcaster и проверкой startup-sweep. | active | `--tracking-id`, `--warmup-seconds` | `python scripts/live_smoke_bot.py --tracking-id demo --warmup-seconds 30` |
| `recompute_outcome_r_multiples.py` | Пересчитывает R-multiple и max-loss по закрытым исходам; применение требует `--apply`. | active | `--db`, `--report`, `--apply` | `python scripts/recompute_outcome_r_multiples.py --report logs/21_outcome_r_recompute.md` |
| `runtime_audit.py` | Глубокий runtime-аудит telemetry/DB с аналитикой воронки. | active | `--run-id`, `--db-path`, `--telemetry-dir` | `python scripts/runtime_audit.py --run-id <RUN_ID>` |
| `telemetry_analyzer.py` | Анализирует tail-limited telemetry JSONL и формирует calibration report. | active | `--telemetry-dir`, `--run-id`, `--hours`, `--output`, `--max-lines-per-file` | `python scripts/telemetry_analyzer.py --hours 72` |
| `validate_config.py` | Проверка консистентности `config.toml` и базовых инвариантов подготовки фич/индикаторов. | active | `--config`, `--symbol`, `--interval`, `--limit` | `python scripts/validate_config.py --config config.toml` |

## Политика по статусам

- **active**: используется в текущем операционном цикле.

## Удалённые ad-hoc скрипты

- `quick_check.py` удалён: использовал хардкод пути и run-id.
- `full_indicators_registry.py`, `generate_audit_artifacts.py`, `migrate_configs.py`, `monitor_runtime.py` удалены: они были legacy-wrapper'ами или дублировали активные live/runtime проверки.
