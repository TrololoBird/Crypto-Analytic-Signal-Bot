# Signal-only product (ручной вход, без исполнения)

Целевой продукт — **аналитический Telegram-канал**: бот **никогда** не открывает, не закрывает и не сопровождает позиции на бирже. Подписчик **сам** вносит ордера у своего брокера.

## 1. Абсолютная граница (non-negotiable)

| Разрешено | Запрещено навсегда |
|-----------|-------------------|
| Public REST/WS Binance (без ключей) | API keys, signed endpoints |
| Публикация trade plan в Telegram | `POST /order`, `POST /batchOrders`, algo orders |
| Tracking исходов по **mark price** (публичный) | Copy-trading, signal copier, MetaAPI auto-exec **внутри бота** |
| Operator dashboard (локально) | Публичный dashboard с trade-кнопками |
| Backtest / sandbox **offline** | Live execution loop |

**Терминология в документации:** слово «trading» в контексте OSS (freqtrade, «trading bot») — это **антипаттерн для сравнения**, не цель продукта. Наш продукт: **signal factory + manual execution by human**.

Упоминания в [SIGNAL_BOT_LANDSCAPE.md](SIGNAL_BOT_LANDSCAPE.md) про «auto-trade from Telegram» — категория **чего не делать**.

## 2. Веб-исследование: что ждут от manual signal channel

| Источник | Вывод для архитектуры |
|----------|----------------------|
| [Mudrex — signal format 2026](https://mudrex.com/learn/best-crypto-signal-providers-on-telegram/) | **Entry zone**, SL, TP1–TP2, invalidation, timeframe; подписчик **manually** на бирже; SL/TP у брокера — ответственность человека |
| [United Kings — execution guide](https://unitedkings.net/how-to-execute-forex-signals-like-a-professional-trader-10/) | 20-сек чеклист: инструмент, late-entry rule, spread, news; **limit vs market** — выбор человека |
| [United Kings — strategy guide](https://unitedkings.net/telegram-forex-signals-the-complete-2025-strategy-guide-8/) | Пропуск если entry ушёл (majors 5–8 pips, gold $2–$4); **signals ≠ план трейдера** без своих правил риска |
| [VoiceOfChain — TG signals](https://voiceofchain.com/academy/telegram-crypto-signals) | Сигнал = данные; решение act/skip/modify; **auto-execute copier** — отдельный риск, не default |
| [tgsignals Pro methodology](https://tgsignals.com/pro/) | Детерминированные entry/SL/TP в канал; **«paste into your broker»**; auto-exec только через **внешнего** EU-партнёра, не в алгоритме канала |
| [MakiBoro — slippage checklist](https://medium.com/@maki53/agent-builder-from-good-signal-to-good-trade-building-a-slippage-first-execution-checklist-5990822a8ee1) | «Good signal» ≠ «good trade»; liquidity gate, slippage budget — бот должен **фильтровать неисполнимые** планы |
| [Darkbot — automate TG](https://darkbot.io/en/blog/automate-trading-in-telegram-crypto-communities-2026-guide) | Промо автоторговли; для нас — **контрпозиция**: ценность в **качестве плана**, не скорости клика |

**Продуктовый вывод:** бот проектирует сообщения так, чтобы человек успел **прочитать → проверить → выставить bracket**; не оптимизируем sub-second HFT.

## 3. Следствия для стратегий и сетапов (не «пороги»)

**Важно:** signal-only — это прежде всего **какие setup_id вообще имеют смысл**, а не только `score += 0.05`.

Полная матрица M1/M2/W/C/R: **[STRATEGY_MANUAL_SUITABILITY.md](STRATEGY_MANUAL_SUITABILITY.md)**.

| Обычный auto-bot / [Crypto-Signal](https://github.com/CryptoSignal/Crypto-Signal) | Signal-only bot |
|------------------|-----------------|
| Точка входа, market order | **Зона entry** (2–3 leg scale-in в TG) |
| Intrabar / 5m `alert_frequency: always` | Публикация на **close** 15m+; WATCH при forming |
| Микро 1m/тик как основной edge | **R-class:** redesign или WATCH-only (`depth_imbalance`, `spread_strategy`, `whale_walls`, `price_velocity`) |
| 500 монет × RSI alerts | Shortlist 40–55 + 7 anchors; 8–15 families per symbol |
| Узкий SL «под API» | SL за структурой + ATR buffer (ручной slippage) |
| Индикатор «RSI hot» без SL | Полный trade plan или **не ACTION** |

**4 setup_id — R-class (no ACTION until redesign):** `price_velocity`, `whale_walls`, `spread_strategy`, `depth_imbalance` — слишком быстрые для ручного входа; см. [CRYPTO_SIGNAL_COMPARISON.md](CRYPTO_SIGNAL_COMPARISON.md).

**Семейства с повышенным приоритетом для канала:** SMC с HTF, multi_tf, funding/OI с confirm bar, anchor symbols (см. [BENCHMARK_ANCHORS.md](BENCHMARK_ANCHORS.md)).

## 4. Следствия для фильтров

1. **Contract:** entry zone width, min R:R TP1 ≥ 1.5, SL/TP обязательны ([signal_contract](SIGNAL_EVALUATION.md)).
2. **Freshness:** stale book/OI → WATCH only или reject.
3. **Spread / book:** сжатый spread без импульса — не ACTION ([slippage-first](https://medium.com/@maki53/agent-builder-from-good-signal-to-good-trade-building-a-slippage-first-execution-checklist-5990822a8ee1)).
4. **Late-entry hint в TG:** «если цена ушла > X% от зоны — skip» (majors ~0.3%, alts ~0.5%, metals шире) — из [United Kings](https://unitedkings.net/telegram-forex-signals-the-complete-2025-strategy-guide-8/).
5. **Caps:** daily ACTION, burst per 15m, 1 ACTION / symbol / 2–4h — защита подписчика от перегруза.
6. **No chase:** не публиковать «догоняющий» сигнал после импульса без retest.

## 5. Следствия для таймфреймов

| Правило | Обоснование (manual) |
|---------|----------------------|
| ACTION только `trigger_tf` ≥ 15m | Человек не успевает на 1m/5m ([TELEGRAM_CHANNEL_SPEC.md](TELEGRAM_CHANNEL_SPEC.md)) |
| `pattern_tf` в сообщении = источник паттерна | Подписчик видит «на чём» построен план |
| HTF в reasons: `Context: 1h up · 4h range` | Ручная проверка HTF за 10 сек ([execution guide](https://unitedkings.net/how-to-execute-forex-signals-like-a-professional-trader-10/)) |
| Session killzone — clock + 15m | Совпадает с ликвидностью London/NY |
| 4h trigger — редкие swing ACTION | Мало постов, длинный TTL |

## 6. Дисклеймер в каждом ACTION

Фиксированный блок (см. TELEGRAM_CHANNEL_SPEC): education only, **no auto-trading**, прошлые результаты ≠ будущие, риск на стороне подписчика.

## 7. Tracking без торговли

- Состояния TP/SL — **информационные** обновления в TG («TP1 hit — consider BE»), не ордера.
- Mark price + kline H/L — публичная верификация track record.
- Diary — ручные сделки оператора, не исполнение бота.
