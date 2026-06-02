# Доступ к Binance API из России

Binance блокирует запросы из ряда регионов (включая РФ) на уровне CDN. Бот использует **только публичные** REST/WebSocket endpoints — приватные ключи и автоторговля не нужны.

## Рекомендуемый подход (без встраивания «чужих» прокси в код)

Не подключайте в репозиторий списки бесплатных публичных прокси: они нестабильны и могут перехватывать трафик.

Вместо этого поднимите **локальный** клиент (один раз на машине):

| Клиент | Локальный SOCKS5 | Регистрация |
|--------|------------------|-------------|
| Clash Verge / Mihomo / v2rayN | `127.0.0.1:7890` | Зависит от подписки/узлов |
| Tor Browser / tor service | `127.0.0.1:9050` | Не нужна (медленно) |
| Корпоративный HTTP proxy | `http://host:8080` | По политике IT |

Бот читает прокси из `config.toml`, `.env` или переменных окружения.

## Настройка

### 1. `config.toml`

```toml
[bot.network]
proxy_url = "socks5h://127.0.0.1:7890"
trust_env = true
```

`trust_env = true` — дополнительно учитывать `HTTPS_PROXY` / `BINANCE_PROXY_URL`.

### 2. `.env` (приоритет через `BINANCE_PROXY_URL`)

```env
BINANCE_PROXY_URL=socks5h://127.0.0.1:7890
```

### 3. PowerShell (сессия)

```powershell
$env:BINANCE_PROXY_URL = "socks5h://127.0.0.1:7890"
python scripts/probe_binance_access.py
```

### 4. Автоподбор локальных портов

```powershell
python scripts/probe_binance_access.py --try-local-ports
```

Скрипт перебирает типичные порты Clash/Tor и выводит рабочий URL.

## Проверка

```powershell
pip install -e ".[live,dev,test]"
python scripts/probe_binance_access.py
$env:PYTEST_LIVE=1; pytest tests/live/test_binance_public_api.py -q
python scripts/live_check_binance_api.py
```

## Live-запуск бота

```powershell
$env:BINANCE_PROXY_URL = "socks5h://127.0.0.1:7890"
python main.py
```

или smoke с Telegram (нужны секреты в `.env`):

```powershell
python scripts/live_smoke_bot.py --minutes 15
```

## WebSocket

Для SOCKS5 установлены зависимости `aiohttp-socks` и `python-socks[asyncio]`. WebSocket использует тот же `proxy_url` / `WSS_PROXY`.

## CI (GitHub Actions)

Раннеры в заблокированных регионах пропускают live-тесты (`tests/live/conftest.py`). Локальная проверка с прокси — обязательна перед продакшен-запуском из РФ.

## Правовая оговорка

Использование VPN/прокси для доступа к зарубежным биржам может регулироваться местным законодательством. Ответственность за соблюдение правил — на операторе бота.
