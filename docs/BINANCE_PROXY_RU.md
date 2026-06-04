# Доступ к Binance API из России (автоматика)

Бот **сам** поддерживает egress для 24/7:

1. При старте: если прямой доступ и настроенный пул недоступны — `scripts/discover_binance_proxies.py` (через `bot.market.proxy_bootstrap`).
2. В рантайме: failover по `proxy_urls` при transport/geo-ошибках (REST + переподключение WS).
3. Прямой доступ: `trust_env = true` — используется, пока не сработает geo-block.

## Обновление пула вручную (агент/CI)

```powershell
.\.venv\Scripts\python.exe scripts\discover_binance_proxies.py --config config.toml
```

Результат пишется в `[bot.network]` в `config.toml` (gitignored у оператора).

## Поля `[bot.network]`

| Поле | Назначение |
|------|------------|
| `proxy_url` | Основной endpoint (пусто = сначала direct при `trust_env`) |
| `proxy_urls` | Цепочка резерва |
| `failover_enabled` | Автопереключение |
| `failover_cooldown_seconds` | Пауза на сбойный endpoint |

## Проверка

```powershell
.\.venv\Scripts\python.exe scripts\probe_binance_access.py --all-configured
```

## Spot companion и лимит weight

Опциональный ``[bot.spot_companion]`` ходит на публичный spot REST (``data-api.binance.vision``) через отдельную aiohttp-сессию и **не** участвует в общем futures weight-budget бота (``bot.market.rate_limit``, ``REST_WEIGHT_PACE_LIMIT``). Запросы spot не «делят» счётчик с ``/fapi`` и ``/futures/data``; при включении companion увеличивайте ``refresh_interval_seconds`` и держите короткий список ``lead_symbols``, иначе spot-IP может упереться в собственные лимиты Binance независимо от прокси для futures.
