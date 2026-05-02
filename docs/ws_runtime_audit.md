# WS runtime audit (public market streams)

Date: 2026-04-28

## Scope

Проверка runtime-механики WebSocket по 5 пунктам:
1) URL construction и маршрутизация публичных market-streams.
2) Подписки и защита от дубликатов/рассинхронизации.
3) Reconnect policy (backoff, retry behavior, reset состояния после восстановления).
4) Health-check / heartbeat и критерии stale.
5) Влияние WS-сбоев на freshness shortlists и момент fallback.

## 1) URL construction и routing публичных market-streams

### Confirmed facts

- В `WSConfig` endpoint-классы `public` и `market` физически разделены: `public_base_url` должен оканчиваться на `/public`, `market_base_url` должен оканчиваться на `/market`. Это соответствует Binance routed endpoint split.
- Фактический stream URL строится как `f"{base}/stream"`, где `base` очищается от суффиксов `/ws` и `/stream`, но сохраняет routed suffix `/public` или `/market`.
- Классификация потока делается через `stream_endpoint_class(...)`:
  - `@bookTicker`/`@depth` → `public`;
  - `@kline_*`, `@aggTrade`, `!ticker@arr`, `!markPrice@arr`, `!miniTicker@arr`, `!forceOrder@arr` → `market`;
  - private/auth паттерны (`listenKey`, `@account`, `@order`, `userdatastream`) явно запрещены `ValueError`.
- `recompute_intended_streams(...)` поддерживает раздельные множества потоков: `manager._intended_streams_by_endpoint["public"]` и `["market"]`, и только затем объединяет их в общий intended set.

### Assumptions / inferences

- Код сознательно закладывает раздельные физические routed URLs для public/market streams. Это подтверждено `WSConfig._resolve_endpoint_urls(...)` и `endpoint_base_url(...)`.

## 2) Подписки, дубликаты и рассинхронизация

### Confirmed facts

- На уровне символов дубликаты убираются при нормализации через `dict.fromkeys(...)` с сохранением порядка.
- При `subscribe(...)` пересчитываются intended sets и отправляются только дельты: `UNSUBSCRIBE` для удалённых stream-ов и `SUBSCRIBE` для добавленных.
- Команды подписок отправляются chunk-ами с троттлингом (`subscribe_chunk_size`, `subscribe_chunk_delay_ms`) и единым возрастающим `id`.
- На WS ack/error:
  - ошибки сохраняются в `self._subscription_errors[endpoint]`;
  - подтверждения считаются в `self._subscription_ack_count[endpoint]`;
  - при `LIST_SUBSCRIPTIONS`-подобном ответе есть debug-проверка mismatch expected vs actual.
- Защита от event-спама/дубликатов:
  - ticker и mark price throttling (минимум по времени между апдейтами);
  - stale-event drop для `bookTicker/aggTrade/24hrTicker/miniTicker/markPriceUpdate` по возрасту `market_ticker_freshness_seconds`.

### Assumptions / inferences

- Для `aggTrade` нет явного dedup по `trade_id` в WS handler (append в deque), то есть упор сделан скорее на bounded buffer + downstream aggregation, чем на строгий idempotency filter.

## 3) Reconnect policy

### Confirmed facts

- Цикл `_run_stream(endpoint)` использует экспоненциальный backoff:
  - старт `delay=1s`;
  - после фейла sleep(delay), затем `delay = min(delay*2, reconnect_max_delay_seconds)`.
- `compute_disconnect_delay(...)` учитывает short-lived streak и тип ошибки:
  - базовый min delay 1s;
  - при streak >=3 / >=5 / >=8 поднимает floor до 5s / 30s / 300s;
  - для keepalive ping timeout floor = 1s;
  - добавляется jitter.
- Сброс backoff/streak:
  - если соединение живёт >=30s, delay сбрасывается на 1s и streak=0.
- Proactive reconnect около 24 часов uptime (`23h50m`), затем reconnect и (для market endpoint) оценка необходимости backfill stale symbols.
- После успешного reconnect:
  - `apply_connected_state(...)` обновляет ws state, endpoint events, counters;
  - очищаются last message timestamps/lag для корректного fresh-state;
  - вызывается reconnect callback со 2-го подключения endpoint (`endpoint_connect_count > 1`).

### Assumptions / inferences

- Явного `max retries` (останов после N попыток) нет: модель «бесконечный retry пока `_running=True`».

## 4) Health-check / heartbeat / stale критерии

### Confirmed facts

- Внутренний WS health monitor (`_health_monitor`) с периодом 30s:
  - если молчание endpoint > `health_check_silence_seconds` и есть intended streams, принудительно `ws.close()` для reconnect.
- Endpoint-specific health (`evaluate_endpoint_health`):
  - `market`: после grace (`market_reconnect_grace_seconds`) при `fresh_tickers==0` или `fresh_mark_prices==0` → принудительный reconnect;
  - `market`: stale kline streams логируются и запускают backfill (без reconnect);
  - `public`: после grace при `fresh_book_tickers==0` (если bookTicker включён) → reconnect.
- Критерий stale для kline stream: age последнего close_time > `interval_seconds * 6`.
- Heartbeat runtime (`HealthManager.heartbeat_periodic`) раз в 300s пишет `ws_lag_ms`, `ws_msg_age_s`, shortlist/open_signals; `health_telemetry_periodic` раз в 60s пишет `state_snapshot()` WS в `health.jsonl`.

### Assumptions / inferences

- Staleness оценивается многослойно: message silence, freshness caches, stale klines. Это уменьшает ложные триггеры от временных всплесков.

## 5) Влияние WS-сбоев на freshness shortlist и fallback

### Confirmed facts

- Короткий shortlist refresh path:
  - если не наступил full refresh interval, используется `build_light_shortlist()` из WS cache (`seed_source="ws_light"`), но только при тёплом ticker cache и наличии symbol meta.
- При холодном WS-кеше или пустом light shortlist происходит `build_live_shortlist()` через REST (`seed_source="rest_full"`).
- При исключениях действует деградация:
  - сначала cached live shortlist (`source="cached"`),
  - затем pinned fallback (`source="pinned_fallback"`).
- Emergency fallback loop (`emergency_fallback_scan`) запускается раз в `emergency_fallback_seconds` и **пропускает** full scan, если:
  - недавно были kline events, или
  - WS flow healthy (`fresh_klines_15m >= 70% tracked` и `stale_kline_stream_count==0`) и ws message age < fallback_sec, или
  - есть backlog event bus, или
  - analysis pool занят.
- Full emergency scan запускается, когда этих условий нет: лог `"no kline events ... running full scan"` + `_run_emergency_cycle()`.

### Assumptions / inferences

- Практический «момент переключения» на fallback определяется не одним флагом disconnect, а комбинацией: отсутствие kline-event активности + деградация WS freshness + отсутствие признаков временной перегрузки (backlog/busy).

## Quick risk notes

- `ReconnectEvent` подписан в `SignalBot`, но в текущем пути WS reconnect callback в EventBus не публикуется напрямую (видно только callback hook). Если требуется обязательная EventBus-семантика reconnect для downstream логики, стоит проверить/усилить это поведение отдельным change-set.
- `aggTrade` WS поток не dedup-ится по `trade_id` в online append path; если провайдер пришлёт повторы, нагрузка на буфер вырастет (хотя bounded deque ограничивает память).
