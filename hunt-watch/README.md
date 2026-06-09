# Hunt Watch

**Memecoin pump/dump minute scanner** — отдельный подпроект основного signal-bot.

- Только **публичные** Binance USDⓈ-M REST endpoints
- **Telegram manual signals** на closed-bar confirm
- **Не auto-trade**, не bypass main-bot delivery path для обычных символов
- Зависит от monorepo: `bot/market`, `bot/features`, `bot/engine`, `config.toml`, `.env`

## Быстрый старт

```bash
# из корня репозитория
source .venv/bin/activate
pip install -e ".[live,dev]"   # подхватывает hunt_watch

# разовый скан universe
python hunt-watch/scripts/scanner.py --print

# minute watch (60s tick, Telegram on confirm)
python hunt-watch/scripts/watch.py --interval 60

# независимая сверка (без hunt-heuristics)
python hunt-watch/scripts/independent_batch.py JCTUSDT BEATUSDT
```

**Секреты:** `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` в `.env` (как у main bot).

**Данные:** `hunt-watch/data/` — watchlist, ticks, signal state, cooldown.

**Legacy wrappers** (совместимость): `scripts/dump_minute_watch.py`, `scripts/hunt_scanner.py`.

## Структура

```
hunt-watch/
├── README.md              ← этот файл
├── ARCHITECTURE.md        ← полный pipeline, условия, стратегия
├── config.defaults.toml   ← пороги (reference)
├── hunt_watch/            ← Python package
│   ├── lifecycle.py       ← FSM фаз пампа/дампа
│   ├── screener.py        ← radar scoring 24h ticker
│   ├── scanner_runner.py  ← scan → watchlist.json
│   ├── targets.py         ← universe merge
│   ├── levels.py          ← entry/SL/TP structural
│   ├── signal_tracker.py  ← latch, follow-up TG
│   └── paths.py           ← data paths
├── scripts/
│   ├── watch.py           ← main loop
│   ├── scanner.py
│   ├── independent_batch.py
│   └── beat_check.py
└── data/                  ← runtime state
```

## Отличие от main bot

| | Main bot | Hunt Watch |
|---|----------|------------|
| Trigger | WS kline close | REST poll 60s |
| Delivery | contract → confluence 3/5 → TG | Hunt confirm → TG |
| Memecoin short | часто `htf_conflict` | hunt confirm + advisory audit |
| Universe | shortlist | pinned + defaults + scanner |

Подробности: [ARCHITECTURE.md](./ARCHITECTURE.md)
