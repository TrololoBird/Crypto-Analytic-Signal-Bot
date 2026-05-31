# Benchmark anchor assets (максимум данных, уверенные сигналы)

Якорные активы задают **режим рынка** и **качество** сигналов на alts. Для них бот собирает **максимум** публичных данных и применяет **более строгие** пороги ACTION.

## 1. Список якорей (целевой)

| Symbol | Роль | Примечание Binance USD-M |
|--------|------|---------------------------|
| **BTCUSDT** | Risk-on/off, beta для alts | Главный benchmark |
| **ETHUSDT** | Alt leadership, BTC/ETH ratio | [OKX: flows BTC↔ETH](https://www.okx.com/learn/btc-eth-correlation) |
| **SOLUSDT** | High-beta L1 proxy | |
| **XRPUSDT** | Ликвидный major, отдельный narrative | Always pinned |
| **XAUUSDT** | Металл / макро risk | TradFi perp; public klines/WS OK; см. [WEB_RESEARCH_SUPPLEMENT.md](WEB_RESEARCH_SUPPLEMENT.md) §3 |
| **XAGUSDT** | Серебро, корреляция с XAU | То же; `GET /fapi/v1/tradingSchedule` для сессий |
| **PAXGUSDT** | Tokenized gold (база **PAXG**, пользовательский «PAX») | Не путать с stable PAX |

**Signal-only bot:** не вызывает `POST /fapi/v1/stock/contract` (USER_DATA для трейдеров). Публичные данные по XAU/XAG — без API keys.

**Всегда:** символы из этого списка **никогда не выпадают** из shortlist при refresh (pinned + `deep_analysis=true`).

## 2. Максимум данных (что собирать 24/7)

Для **каждого** якоря, независимо от динамического shortlist:

| Канал | Частота | Зачем |
|-------|---------|-------|
| WS `@kline_5m/15m/1h/4h` | continuous | MTF + все `required_tfs` стратегий |
| REST klines backfill | startup + gap | 30+ баров на каждый TF |
| `!markPrice@arr` + symbol mark | WS | Funding, tracking |
| `premiumIndex` / `fundingRate` hist | 5–15 min | funding_reversal, confluence |
| `openInterest` + `openInterestHist` | 5–15 min | oi_divergence |
| `globalLongShortAccountRatio` + top L/S | batch 5–15 min | ls_ratio, crowd |
| `takerlongshortRatio` | batch | aggression_shift |
| `@aggTrade` или ring buffer | WS | CVD, absorption |
| `@depth20@500ms` или REST depth | top priority | depth_imbalance, walls |
| `!bookTicker` / spread | WS | spread_strategy filter |
| `!forceOrder@arr` | WS | liquidation context |

**Universe ticker24h:** якоря не зависят от «выпали из топ-50 по объёму» — защита в `universe` (pinned_rows).

## 3. Benchmark context для всех alts

```text
benchmark_context = prepare_anchors(BTC, ETH, SOL, XRP, XAU, XAG, PAXG)
  → btc_bias, btc_phase, eth_btc_ratio, altseason proxy, macro_risk_mode
```

Каждый `prepare_symbol(ALT)` **обязан** получать свежий `benchmark_context` (< 15 min).

Стратегии: `btc_correlation`, `altcoin_season_index`, фильтры «не long alts в bitcoin season без confirm».

**Веб:** корреляция BTC–ETH 0.75–0.90 ([FullSwing 2025](https://www.fullswing.ai/blog/crypto-correlation-trading-strategies)); breakdown < 0.6 — regime shift; для signal-bot — **фильтр направления**, не pair-trade execution.

## 4. «Уверенные сигналы» на якорях (продукт)

Якоря — **лица канала**; ACTION на BTC/ETH/XAU с высокой планкой:

| Параметр | Alts (dynamic shortlist) | Anchors |
|----------|--------------------------|---------|
| Min `final_score` ACTION | config default | **+0.03…0.05** выше |
| Hard confluence | ≥ 3/5 | **≥ 4/5** опционально для metals |
| Min R:R TP1 | 1.5 | **1.6–1.8** на XAU/XAG |
| Entry zone width | уже | majors уже; metals **шире** (волатильность) |
| ACTION cap share | — | не > 40% всех ACTION с одного anchor за день |

**WATCH** на якорях допускается свободнее (радар режима).

## 5. Таймфреймы по якорю (ось актива)

| Asset | primary_timeframe (TG) | context_timeframes | min_trigger_tf ACTION |
|-------|------------------------|--------------------|-------------------------|
| BTC, ETH, SOL, XRP | 15m | 1h, 4h | 15m |
| XAU, XAG, PAXG | **1h** | 4h, 15m | 15m (pattern), presentation 1h |

Стратегии с `trigger_tf=15m` на XAU **всё ещё работают** на 15m close; в TG подпись «· 1h context».

## 6. Gap bot2 → target

| Target | bot2 сейчас |
|--------|-------------|
| XRPUSDT pinned + deep_analysis | **Нет** в `_DEEP_ANALYSIS_PRIORITY_SYMBOLS` |
| Все 7 якорей max enrich | Частично (6 символов без XRP) |
| Stricter ACTION on anchors | Нужен config `assets.*.action_score_floor` |

См. [GAP_ANALYSIS_BOT2.md](GAP_ANALYSIS_BOT2.md).
