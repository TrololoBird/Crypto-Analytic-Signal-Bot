# Web research supplement (May 2026)

Дополнение к target spec по **самостоятельно выбранным** темам. Источники — официальная документация Binance, Bot API, индустриальные гайды, OSS. **Не** сверка с legacy-кодом.

См. [PLAN_CRITICAL_REVIEW.md](PLAN_CRITICAL_REVIEW.md) §0 (методология).

---

## 1. Binance USD-M: ликвидации `!forceOrder@arr`

| Факт | Источник |
|------|----------|
| Update speed **1000ms** | [Liquidation Order Streams](https://developers.binance.com/docs/derivatives/usds-margined-futures/websocket-market-streams/Liquidation-Order-Streams) |
| На символ за интервал — **крупнейший** liquidation order (changelog: «largest», не «latest») | [Change Log](https://developers.binance.com/docs/derivatives/change-log) |
| Если ликвидаций нет — **нет push** | Official stream description |

**Spec implication ([ORDER_FLOW_INGEST.md](ORDER_FLOW_INGEST.md)):** `liquidation_score` = **proxy heatmap**, не полная лента. Для ACTION использовать как confluence, не как единственный триггер.

---

## 2. Binance WS лимиты (планирование подключений)

| Лимит | Значение | Источник |
|-------|----------|----------|
| Streams / connection | **1024** max | [USD-M Connect](https://developers.binance.com/docs/derivatives/usds-margined-futures/websocket-market-streams/Connect) |
| Incoming messages | **10 / sec** | Same |
| Reconnect | **24h** | General Info |
| Ping | каждые **3 min**, pong в **10 min** | WS API General Info |

**Spec implication:** бюджет ~74–124 streams для N=50 в [BINANCE_PUBLIC_DATA_MATRIX.md](BINANCE_PUBLIC_DATA_MATRIX.md) — **валиден**; шардировать при N>80 или depth на все символы.

---

## 3. TradFi perps XAU / XAG (якоря)

| Факт | Источник |
|------|----------|
| **XAUUSDT**, **XAGUSDT** — regulated TradFi perpetuals, USDT-settled, 24/7 | [Binance PR Jan 2026](https://www.prnewswire.com/news-releases/binance-launches-first-regulated-tradfi-perpetual-contracts-settled-in-stablecoin-starting-with-gold-and-silver-302656186.html) |
| **Public market data** (klines, mark, WS) — без ключей | Standard `/fapi/v1/*` + streams |
| **POST `/fapi/v1/stock/contract`** — USER_DATA, one-time **trader** agreement | [TradFi-Perps API](https://developers.binance.com/docs/derivatives/usds-margined-futures/trade/rest-api/TradFi-Perps) |

**Spec implication ([BENCHMARK_ANCHORS.md](BENCHMARK_ANCHORS.md)):** signal-only бот **не** вызывает stock/contract; якоря включают XAU/XAG в always-on public ingest. В TG disclaimer: «TradFi perps — отдельные правила биржи для ручной торговли».

**Доп.:** `GET /fapi/v1/tradingSchedule` — сессии TradFi (changelog); учесть в freshness для metals.

---

## 4. Telegram delivery limits

| Лимит (community / PTB constants) | Значение | Примечание |
|-----------------------------------|----------|------------|
| Per chat | ~**1 msg/s** | [python-telegram-bot FloodLimit](https://github.com/python-telegram-bot/python-telegram-bot/blob/v22.7/telegram/constants.py) |
| Global burst | ~**30 msg/s** across chats | Same |
| Group | ~**20 msg/min** | Same |
| Paid broadcast | до **1000 msg/s** (Stars) | Bot API 7.1+ |

**Spec implication ([TELEGRAM_CHANNEL_SPEC.md](TELEGRAM_CHANNEL_SPEC.md)):**

- **15–40 ACTION/day** + **8–15 burst/15m** — согласуется с лимитами и с **quality-first** каналами (3–8 до 8–15 у «профи» агрегаторов), против 50+/day spam-моделей.
- **WATCH silent** — отдельный канал или `disable_notification` — снижает 20/min group risk.
- Delivery queue: token bucket, обработка `429` + `retry_after`.

**Веб-контекст cadence:** PrimeXBT/обзоры 2026 — легитимные crypto groups часто **1–4** setups/day; агрегаторы заявляют **8–15**; high-frequency VIP **20–50** — маркетинг, anti-pattern для manual execution ([primexbt.com](https://primexbt.com/for-traders/20-best-crypto-signals-telegram-groups/), [altsignals.io](https://altsignals.io/signals/crypto-signals)).

---

## 5. Universe screener (light → shortlist)

**Паттерн из OSS/гайдов:**

| Этап | Фильтры (примеры) | Источник |
|------|-------------------|----------|
| Light scan 150–200 | `quoteVolume24h` min, spread, listing age | [FXonbit screener guide](https://fxonbit.com/blog/filtering-high-volume-crypto-with-screener/), gunbot-quant |
| Shortlist 40–55 | + ATR regime, BTC correlation bucket, wash-volume heuristic | [quantflow](https://github.com/Snack-JPG/quantflow), [market-signals](https://github.com/thrive-fi/market-signals) |
| Deep path | aggTrade, depth, OI batch, 8–15 lanes | PROJECT_ARCHITECTURE |

**Spec implication:** добавить в PROJECT_ARCHITECTURE §universe явные **пороги старта** (калибруемые):

- Min 24h quote volume: **$50M–100M** (alts), без floor на 7 anchors
- Max spread bps: **15–25** (config)
- Refresh: light **60–120s**, deep **2–4h**

---

## 6. SMC / FVG (15m ACTION)

**Консенсус ICT-гайдов 2025–2026:**

| Правило | Деталь | Источник |
|---------|--------|----------|
| HTF bias | 4H/Daily перед 15m entry | [quantum-algo FVG guide](https://www.quantum-algo.com/blog/guides/fair-value-gaps-complete-trading-guide/) |
| Entry zone | **50% CE** FVG или retest после **LTF MSS** | [ictkillzone.com](https://www.ictkillzone.com/ict-fair-value-gap) |
| Фильтры | Liquidity sweep before displacement; kill zone; не mitigated gap | Same |
| SL | За дальний край FVG / sweep wick | innercircletrader.net |

**Spec implication ([STRATEGY_CATALOG.md](STRATEGY_CATALOG.md) card `fvg_setup`):** ACTION только при `htf_aligned` + `sweep_before_displacement` + close 15m; WATCH при touch без MSS.

---

## 7. Trust / public audit ledger

| Практика | Источник |
|----------|----------|
| Timestamped history **с losses** | [Markets Herald](https://marketsherald.com/how-to-find-legit-crypto-telegram-influencers-and-avoid-scams/), [CoinBrain](https://devel.coinbrain.com/blog/top-crypto-signal-groups) |
| Независимый log до подписки 30–60 дней | [Bitget Academy 2026](https://www.bitget.com/academy/crypto-signals-teleg) |
| SHA256 daily CSV + archive snapshot | [puc-telegram audit guide](https://puc-telegram.com/blogs/562/) |

**Spec implication (innovation backlog):** `signals_YYYYMMDD.csv` + SHA256 в pinned post; journal row **до** TG send (immutable order).

---

## 8. Решения, усиленные этим проходом

| Область | Решение spec |
|---------|----------------|
| Liquidation ingest | Proxy only; label in TG «liq cluster (1s snapshot)» |
| TG caps | 15–40 ACTION, queue + 429 handling |
| Anchors | 7 symbols incl. XRP; XAU/XAG public OK |
| Screener | Documented volume/spread thresholds |
| FVG | HTF + sweep + CE/MSS rules in catalog |
| Audit | Pre-send journal + daily hash |

---

## 9. Готовность к реализации

| Критерий | Статус |
|----------|--------|
| Product boundary | OK |
| Data matrix + WS budget | OK (verified limits) |
| 38 strategy catalog + manual classes | OK |
| Evaluation + collision + TG spec | OK |
| Connector | OK |
| Web gaps (this doc) | **Closed** |
| Implementation order | [GAP_ANALYSIS_BOT2.md](GAP_ANALYSIS_BOT2.md) P0–P3 |

**Следующий шаг:** явный sign-off пользователя → execute P0 (scheduler + screener + lanes).
