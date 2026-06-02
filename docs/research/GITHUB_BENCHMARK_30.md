# GitHub benchmark — 30 репозиториев (июнь 2026)

> Метод: GitHub Search API (`scripts/_github_bot_survey.py`, 59 уникальных репо) + curated lists  
> ([awesome-crypto-trading-bots](https://github.com/botcrypto-io/awesome-crypto-trading-bots),  
> [best-of-algorithmic-trading](https://github.com/TitanFlow-Systems/best-of-algorithmic-trading),  
> [awesome-crypto-trading-agents](https://github.com/shakeebshaan/awesome-crypto-trading-agents)).  
> Счётчик `.py` — дерево `HEAD` без `tests/` (где измерялось).  
> **bot2:** 233 файла, ~65k LOC — для сравнения в каждой строке.

Легенда bucket: **P** = popular (stars), **F** = fresh (push 2026-06), **D** = discussed (open issues / forks).

---

## A. 10 популярных (⭐, фреймворки + Binance futures)

| # | Repo | ⭐ | Forks | Issues | ~.py | Архитектура | vs bot2 |
|---|------|---:|------:|-------:|-----:|-------------|---------|
| P1 | [freqtrade/freqtrade](https://github.com/freqtrade/freqtrade) | 51 033 | 10 631 | 35 | **343** | Один пакет `freqtrade/`; стратегии — **плагины** в `user_data/strategies/`, ядро не дублирует detect | Много файлов, но **без двойного дерева** strategies+detectors |
| P2 | [hummingbot/hummingbot](https://github.com/hummingbot/hummingbot) | 18 726 | 4 704 | 343 | **~1547** | MM/arbitrage; коннекторы бирж + стратегии в подпакете; Cython | Другой класс продукта; файлов много, структура **одна** |
| P3 | [jesse-ai/jesse](https://github.com/jesse-ai/jesse) | 7 968 | 1 132 | ~140 | **548** | Один пакет `jesse/`; стратегии = классы; backtest+live один API | Эталон «38 стратегий без 76 файлов» |
| P4 | [Drakkar-Software/OctoBot](https://github.com/Drakkar-Software/OctoBot) | 6 014 | 1 197 | ~800 | **~1829** | Tentacles/плагины, UI, grid/DCA/AI | Модульный, но не 2× каталог на setup |
| P5 | [Superalgos/Superalgos](https://github.com/Superalgos/Superalgos) | ~5 400 | ~1 k | — | Node+visual | Drag-and-drop designer, не Python-first | Не сравнивать по `.py` |
| P6 | [vnpy/vnpy](https://github.com/vnpy/vnpy) | ~25 k | — | — | **сотни** | Event engine + gateway plugins (классический quant CN) | Framework, не signal-only |
| P7 | [QuantConnect/Lean](https://github.com/QuantConnect/Lean) | ~12 k | — | — | **тысячи** | C# LEAN + Python research layer | Institutional; не OSS «бот в 10 файлах» |
| P8 | [enarjord/passivbot](https://github.com/enarjord/passivbot) | 1 989 | 651 | **70** | средний | Futures multi-exchange (Binance, Bybit, …), config-driven | Активное community, один продуктовый код |
| P9 | [Erfaniaa/binance-futures-trading-bot](https://github.com/Erfaniaa/binance-futures-trading-bot) | 400 | 94 | 7 | **9** | **Монолит:** `main.py` ~42k LOC, Telegram, стратегии в конфиге/функциях | **Ближе всего по духу:** мало файлов, много логики в одном месте |
| P10 | [51bitquant/binance_grid_trader](https://github.com/51bitquant/binance_grid_trader) | 955 | 294 | 15 | ~30–50 | Grid Spot+Futures, несколько модулей | Узкий продукт, без 38 parallel detectors |

---

## B. 10 свежих (push 2026-05/06, futures / Polars / signal)

| # | Repo | ⭐ | Forks | Updated | ~.py | Архитектура | vs bot2 |
|---|------|---:|------:|---------|-----:|-------------|---------|
| F1 | [freqtrade/freqtrade](https://github.com/freqtrade/freqtrade) | 51 033 | — | **2026-06-02** | 343 | Активная разработка (develop) | — |
| F2 | [Whit1985/Binance-Futures-Signal-Bot](https://github.com/Whit1985/Binance-Futures-Signal-Bot) | 0* | 0 | **2026-04-05** | мало | `config.json`: 4 стратегии, TG/TradingView, multi-CEX | Signal-first; **нет 80 файлов на setup** |
| F3 | [conor19w/Binance-Futures-Trading-Bot](https://github.com/conor19w/Binance-Futures-Trading-Bot) | 653 | 196 | 2026-05-29 | ~15–40 | TA (EMA, Stoch RSI, …), Tkinter GUI | Классический «несколько стратегий в одном дереве» |
| F4 | [cunarist/solie](https://github.com/cunarist/solie) | 60 | 12 | **2026-06-01** | **73** | Polars, `package/`, GUI, Binance futures | Polars как у bot2, но **1 пакет** |
| F5 | [Wayy-Research/wrtrade](https://github.com/Wayy-Research/wrtrade) | 2 | 1 | 2026-02 | ~10 | Polars: `signal(prices) -> Portfolio`, backtest lib | Библиотека, не 233-file bot |
| F6 | [nikkisora/PolarBT](https://github.com/nikkisora/PolarBT) | 6 | 0 | **2026-03** | малый | Polars backtest `Engine`+`Strategy` | Свежий Polars TA, не production bot |
| F7 | [Yvictor/polars_backtest_extension](https://github.com/Yvictor/polars_backtest_extension) | 6 | 2 | 2026-01 | Rust+Py | `df.bt.backtest()` — расширение Polars | Только backtest слой |
| F8 | [marahman30104/binance-scalping](https://github.com/marahman30104/binance-scalping) | 10 | — | **2026-06-01** | малый | Scalping, узкий scope | Домашнее задание / малый бот |
| F9 | [resoy-33/hyperliquid-bot](https://github.com/resoy-33/hyperliquid-bot) | 1 | — | **2026-06-02** | малый | Hyperliquid (не Binance) | Свежий, другая биржа |
| F10 | [wavyjay1/cross-exchange-arbitrage](https://github.com/wavyjay1/cross-exchange-arbitrage) | 1 | — | **2026-06-02** | малый | Cross-exchange arb | Узкий продукт |

\* новые репо часто 0⭐ при свежем push — смотреть код, не stars.

---

## C. 10 обсуждаемых (issues / forks / strategy ecosystem)

| # | Repo | ⭐ | Forks | Issues | ~.py | Архитектура | vs bot2 |
|---|------|---:|------:|-------:|-----:|-------------|---------|
| D1 | [enarjord/passivbot](https://github.com/enarjord/passivbot) | 1 989 | 651 | **70** | средний | См. P8 | Community-driven config |
| D2 | [iterativv/NostalgiaForInfinity](https://github.com/iterativv/NostalgiaForInfinity) | 3 259 | 730 | **64** | 1–5 | **Одна** mega-strategy для Freqtrade (плагин) | Правильный паттерн: 1 setup = 1 артефакт |
| D3 | [freqtrade/freqtrade-strategies](https://github.com/freqtrade/freqtrade-strategies) | 5 202 | 1 409 | 11 | коллекция | Репозиторий **только стратегий** (не второе ядро) | Аналог: вынести setups **или** strategies, не оба |
| D4 | [cunarist/solie](https://github.com/cunarist/solie) | 60 | 12 | **16** | 73 | См. F4 | Issues > stars → активная доработка |
| D5 | [yeahrb/CEX-Option-Futures-Crypto-Quant-Algorithm-Trading-Bot](https://github.com/yeahrb/CEX-Option-Futures-Crypto-Quant-Algorithm-Trading-Bot) | 481 | 29 | 10 | ? | Multi-CEX quant + Telegram | Маркетинговый README |
| D6 | [TheFourGreatErrors/alpha-rptr](https://github.com/TheFourGreatErrors/alpha-rptr) | 669 | 83 | 2 | ~20 | Binance/Bybit/BitMEX/FTX futures Python | Компактный auto-trader |
| D7 | [ivopetiz/algotrading](https://github.com/ivopetiz/algotrading) | 1 599 | 197 | 8 | средний | Crypto algo **framework** | Framework, не 38× duplicate |
| D8 | [CryptoSignal/Crypto-Signal](https://github.com/CryptoSignal/Crypto-Signal) | ~5 500 | ~1 k | archived | ~30 | **Signal-only** (notifier), индикаторы модулями | Близко по продукту; **мало файлов** |
| D9 | [ctubio/Krypto-trading-bot](https://github.com/ctubio/Krypto-trading-bot) | ~3 700 | — | — | C++ | HFT market making, self-hosted | Не Python |
| D10 | [shivpatel-dev/binance-futures-bot](https://github.com/shivpatel-dev/binance-futures-bot) | малый | — | — | **1** | Telegram channel → parse → Binance (**один файл**) | Крайность: монолит 1 file |

**Доп. Telegram→Binance (curated):** [pawelmat142/binance-bot](https://github.com/pawelmat142/binance-bot) — сигналы из TG в futures; структура модульная, но порядок **10–30 .py**, не 233.

---

## Сводка: откуда 233 файла у bot2

| Источник файлов | ~файлов | У типичного OSS |
|-----------------|--------:|-----------------|
| `setups/detectors/*` + `strategies/*` (дубль 36 setup) | **~85** | 0–38 (один каталог) |
| `market/`, `runtime/`, `persistence/`, `delivery/` | ~70 | 5–25 (в одном пакете) |
| `features/prepare_*` split | ~7 | 1–3 |
| `diagnostics/`, `dashboard/` | ~21 | 0–5 (опционально) |
| остальное | ~50 | — |

**Ни один из 30 репозиториев** не держит **два полных дерева** детекторов (`strategies/` + `setups/detectors/`) с теми же `setup_id`.

---

## Как воспроизвести опрос

```powershell
python scripts/_github_bot_survey.py
# → scripts/_github_bot_survey_out.json (59 repos)
```

Поисковые запросы: `binance futures trading bot`, `crypto signal bot telegram binance`, `algorithmic trading bot crypto`, `polars trading bot`, `freqtrade` (comments), fresh sort по `updated`.

---

## Вывод для de-bloat

1. **Норма для signal/futures bot:** 10–80 `.py` (Erfaniaa 9, Freqtrade 343 как framework-исключение).  
2. **Норма для 38 стратегий:** плагины или spec+registry — **не 76+ файлов-дублей**.  
3. **bot2 цель:** ~70–110 `.py` после удаления дубля и merge split (см. `REFACTOR_PLAN.md` §7).
