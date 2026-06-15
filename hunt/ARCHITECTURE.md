# Hunt Watch — Architecture & Strategy

Подсистема **memecoin pump/dump hunt**: REST minute poll + live WS (liq/aggTrade) + spot lead-lag, Telegram on confirm.  
Отделена в `hunt/`; использует data plane основного бота (`bot/market`, `bot/features`, `bot/engine`).

---

## 1. Назначение и границы

### Что делает
- Находит волатильные USD-M пары (radar → watchlist → minute watch)
- Классифицирует **фазу** импульса (exhaustion, distribution, dump, bounce, recovery)
- Считает **score** short/long setup на closed bars
- Шлёт **Telegram** только при `confirmed` + gates
- Ведёт **active signal tracker** с structural invalidation (не score-flicker)

### Что не делает
- Не ставит ордера, не использует private Binance API
- Не заменяет main-bot delivery path (`validate_signal_contract` → `hard_confluence_gate` → deliver)
- Для pinned hunt-символов TG идёт по hunt-heuristic даже при `htf_conflict` в main bot (advisory audit в сообщении)

### Уроки, заложенные в дизайн
| Кейс | Урок | Fix |
|------|------|-----|
| **VELVET** short | Fade на impulse high + cascade = valid | Confirm на 1m/5m bear cascade |
| **BEAT** short | Sticky confirm после −8% → squeeze | Lifecycle invalidate + local support |
| **JCT** short | Fade на вершине (pos 92%, fall 2%) + мгновенный confirm_lost | `blocks_premature_exhaustion_short` + latch |

---

## 2. Pipeline (end-to-end)

```mermaid
flowchart TB
    subgraph discovery [Discovery — каждые 900s]
        T[Binance 24h ticker ALL USD-M]
        S[hunt_watch.screener rank_hunt_candidates]
        W[hunt_watchlist.json]
        T --> S --> W
    end

    subgraph universe [Universe merge — каждый tick]
        P[pinned from config.toml]
        D[DEFAULT_SYMBOLS]
        W --> M[resolve_watch_universe]
        P --> M
        D --> M
        M --> U[active symbols + watch_mode per symbol]
    end

    subgraph tick [Minute tick — каждые 60s per symbol]
        U --> R[REST klines 1m/5m/15m/1h/4h/1d]
        R --> F[prepare_symbol + indicators]
        F --> L[assess_hunt_lifecycle FSM]
        L --> A[_dump_analysis / _long_analysis]
        A --> C[_confirm_dump / _confirm_long]
        C --> G[gates: premature_exhaustion / lifecycle]
        G --> TG{confirmed → evaluate_delivery?}
        TG -->|yes| Send[Telegram entry + register_signal_open latch]
        TG -->|no| Log[watch_tick / watch_alert_blocked / forming]
        Send --> TR[evaluate_followups structural invalidate / TP / SL warn]
    end

    discovery --> universe
```

### Шаги по порядку

1. **Scanner** (`scanner_runner.run_scan`, каждые 900s из watch loop)
   - Все 24h tickers → `score_hunt_row` → top-N в `hunt/data/hunt_watchlist.json`

2. **Universe** (`targets.resolve_watch_universe`)
   - Merge: `config.universe.pinned_symbols` → `DEFAULT_SYMBOLS` → watchlist (score ≥ 45 или `suggest_minute_watch`)
   - Cap: `MAX_DYNAMIC_SYMBOLS + len(pinned)`
   - Mode: pinned table + scanner `watch_bias`; `effective_watch_mode` учитывает lifecycle

3. **Symbol tick** (`watch._snapshot_symbol`, timeout 180s)
   - REST: klines, OI, funding, taker, basis, depth, LS ratios
   - Feature prep (Polars, Wilder RSI, ATR)
   - Main bot engine: 28 strategies → delivery audit (advisory)

4. **Lifecycle FSM** (`lifecycle.assess_hunt_lifecycle`)
   - Фаза → `recommended_bias`, `short_entry_ok`, `invalidate_short`

5. **Analysis + Confirm**
   - Short: `_dump_analysis` → score + triggers → `_confirm_dump` → `apply_short_invalidation`
   - Long: `_long_analysis` → `_confirm_long`

6. **Delivery gate** (`evaluate_delivery` confirm / `evaluate_forming_gate` forming)
   - Confirm: contract → must_pass → family_vote → `run_gate_pipeline` (single path via dispatch)
   - Forming: telemetry only when gate blocks below forming min score

7. **Telegram**
   - Cooldown 45 min per `symbol:direction`
   - On send: `register_signal_open(telegram_sent=True)` с support/invalidation levels

8. **Follow-ups** (`signal_tracker.evaluate_followups`)
   - Только для `telegram_sent` active signals
   - Invalidate: lifecycle bounce, structural reclaim, SL hit — **не** `confirm_lost`

9. **Persistence**
   - `dump_minute_watch.jsonl` — full tick rows
   - `hunt_signal_state.json` — active/closed signals
   - `dump_watch_telegram_state.json` — entry cooldown

---

## 3. Архитектура модулей

```
hunt/hunt_watch/
├── paths.py           # DATA paths (single source)
├── screener.py        # 24h ticker scoring (radar)
├── scanner_runner.py  # batch scan → watchlist JSON
├── targets.py         # universe + watch_mode merge
├── lifecycle.py       # HuntPhase FSM + gates
├── levels.py          # structural entry/SL/TP (fib + swing)
├── signal_tracker.py  # latch + follow-up events
└── bootstrap.py       # sys.path for scripts

hunt/scripts/watch.py   # orchestrator (~2700 LOC)
  ├── imports engine.market, engine.features, engine.domain, engine.telegram
  └── hunt-specific logic inline: _dump_analysis, _confirm_*, _format_telegram
```

### Зависимости (shared kernel only)

| Hunt Watch | engine module | Зачем |
|------------|---------------|-------|
| watch.py | `engine.market.data` | REST klines, ticker, OI, L/S, funding |
| watch.py | `engine.features.prepare` | Polars frames, indicators |
| watch.py | `engine.telegram` | TelegramBroadcaster |
| watch.py | `engine.domain.config` | load_settings, proxy |
| hunt_watch | **never** `bot.*` | Hunt — отдельный продукт |

---

## 4. Discovery (Radar)

**Файл:** `screener.py`

### Фильтры candidacy
- `quote_volume ≥ $10M` / 24h
- `hunt_score ≥ 25` после scoring

### Scoring (0–100)

| Фактор | Баллы | Flag |
|--------|-------|------|
| \|change_24h\| ≥ 15% | +30 | pump_extreme |
| \|change_24h\| ≥ 8% | +18 | range_hot |
| range_24h ≥ 25% | +20 | range_expansion |
| pos_in_range ≥ 0.85 | +15 | pos_near_high |
| pos_in_range ≤ 0.25 | +12 | pos_near_low |
| log10(volume) bonus | до +10 | — |
| move ≥ 25% + vol ≥ $50M | +8 | liquid_mover |

### Watch bias (scanner)
- pos ≥ 0.85 → **short**
- pos ≤ 0.25 и change ≤ −8% → **long**
- change ≥ +15% → **short**; ≤ −15% → **long**
- иначе → **both**

### Пороги watchlist
- `score ≥ 45` → в watchlist
- `score ≥ 60` → `suggest_minute_watch` (priority)

---

## 5. Lifecycle FSM (фазы)

**Файл:** `lifecycle.py` — `HuntPhase`

**Миссия:** ловить **начало изначального пампа** (impulse/mega-leg) и **начало дампа** (exhaustion → structural break). Mega-leg (leg_gain ≥80%, BEAT 1.5→8.37) ≠ post_dump_bounce.

```mermaid
stateDiagram-v2
    [*] --> no_setup
    no_setup --> breakout_arming: squeeze AND pos 30-65%
    no_setup --> impulse_initiating: leg rally taker_buy
    breakout_arming --> impulse_initiating: broke resistance
    impulse_initiating --> mega_leg_continuation: leg_gain>=80% fall<18%
    mega_leg_continuation --> exhaustion_at_high: near_high AND rsi OB
    no_setup --> exhaustion_at_high: near_high AND rsi_1h>=65
    exhaustion_at_high --> distribution: topping bear structure
    exhaustion_at_high --> dump_active: fall>=6% taker_sell
    dump_active --> post_dump_bounce: true dump bounce non-mega-leg
    post_dump_bounce --> recovery
```

| Фаза | Bias | Продуктовый смысл |
|------|------|-------------------|
| `breakout_arming` | long | База / squeeze перед пробоем |
| `impulse_initiating` | long | Старт или середина импульса вверх |
| `mega_leg_continuation` | long | Продолжение +80% ноги (не bounce) |
| `exhaustion_at_high` | short | Вершина — prep дампа |
| `dump_active` | wait | Mid-dump — monitor only |
| `post_dump_bounce` | long | Отскок после **настоящего** дампа (не mega-leg) |

Early alerts: `hunt_watch/early_alert.py` (PUMP/DUMP PREP/START до confirm).

### Поля HuntLifecycle

| Поле | Смысл |
|------|-------|
| `short_entry_ok` | Можно **новый** TG short (exhaustion, distribution) |
| `short_confirm_ok` | Confirm не demote (не bounce/recovery) |
| `invalidate_short` | Bounce/recovery/accumulation → закрыть short |
| `fall_from_high_pct` | (hunt_high − price) / hunt_high |
| `bounce_from_low_pct` | (price − session_low) / session_low |
| `local_support` | pivot low 5m/15m (не impulse high!) |

### Support break level
- `exhaustion_at_high` → `impulse_high × 0.998`
- иначе → `local_support` (BEAT fix)

### apply_short_invalidation
Demote `confirmed` если lifecycle bounce или late short (fall ≥ 10% без entry_ok).

---

## 6. Стратегия: SHORT (dump fade)

### Impulse context
- **Fast alts** (JCT, BEAT, VELVET): swing на **1h**, window 48 bars
- **BTC**: swing на **4h**, window 30 bars
- `hunt_high` / `hunt_low` — leg для fib и fall_from_high

### Score `_dump_analysis` (triggers)

| Trigger | Баллы |
|---------|-------|
| rsi15 ≥ 72 | +12 |
| rsi1h ≥ 72 | +10 |
| bear RSI div 4h/1h | +15 / +12 |
| 1m rejection wick | +16 |
| 5m rejection wick | +14 |
| 15m rejection | +10 |
| at fib 1272 | +10 |
| extended above impulse high | +8 |
| **5m below support** | **+28** |
| below impulse_high×0.998 | +12 |
| taker sell (<0.98) | +10 |
| oi flush | +10 |
| microprice sell | +8 |
| regime 4h bear | +8 |
| funding > 0.05% | +6 |
| bot_short hits | +6 each, max 18 |

### Confirm `_confirm_dump` (все closed bar)

Hard confirms (любой набор):
- `5m_close_below_support`
- `15m_close_below_support`
- `5m_rejection_exhaustion` (bear + wick + rsi15≥65)
- `1m_5m_bear_cascade`

**Confirmed =** `hard` non-empty **AND** score ≥ 60 **AND** (bear div **OR** score ≥ 68 **OR** oi_flush)

### Gate: premature exhaustion (JCT fix)

**Не слать TG short** если одновременно:
- phase = `exhaustion_at_high`
- `fall_from_high < 5%`
- `pos_in_range > 0.85`
- `bounce_from_low > 15%`
- нет bear div 1h/4h

### Levels `_dump_analysis` → `structural_short_levels`
- **Entry zone:** price .. impulse_high×0.995
- **SL:** max(impulse_high×1.006, entry + 1.1×ATR15)
- **TP1:** fib 0.382 retrace (или mid leg)
- **TP2:** fib 0.5 / impulse_low
- **Invalidation:** SL level (reclaim above → close signal)

---

## 7. Стратегия: LONG (bounce / recovery)

### Score `_long_analysis`

| Trigger | Баллы |
|---------|-------|
| rsi15 ≤ 32 | +12 |
| rsi1h ≤ 35 | +10 |
| bull div 4h/1h | +15 / +12 |
| 1m/5m/15m bounce wick | +16 / +14 / +10 |
| at fib support (ret_382) | +10 |
| deep below impulse_low | +8 |
| reclaim resistance (impulse_high×0.998) | +12 |
| taker buy, oi build, micro buy | +8–10 |
| bot_long hits | +6 each |

### Confirm `_confirm_long`
- Hard: 5m/15m close above resistance, bull wick + rsi, 1m_5m_bull_cascade
- score ≥ 60 AND (bull div OR score ≥ 68 OR oi_build)

### Levels `structural_long_levels`
- **SL:** below impulse_low / local_support − ATR
- **TP1:** local_resistance / impulse_high
- **TP2:** fib 1272 extension

### Invalidate long (follow-up)
- phase → `exhaustion_at_high` or `distribution` после bounce entry

---

## 8. Таймфреймы

| TF | Роль |
|----|------|
| **1m** | session 24h stats, 1m closed candle, cascade |
| **5m** | **primary confirm**, rejection, support break |
| **15m** | confirm, RSI, ATR for levels |
| **1h** | impulse swing (fast alts), RSI, div |
| **4h** | impulse swing (BTC), regime, div |
| **1d** | fib anchors (90 bars) |

**Confirm = closed bars only** (не live wick на forming candle).

---

## 9. Signal tracker (latch)

### Register
Только после **успешного Telegram** (`telegram_sent=True`):
- entry_lo/hi, SL, TP1/TP2
- support_break_level, invalidation_above/below
- lifecycle_phase at open

### Follow-up events

| Event | Условие |
|-------|---------|
| `invalidate` (short) | lifecycle.invalidate_short |
| `invalidate` (short) | price > invalidation_above × 1.001 |
| `invalidate` (short) | price ≥ stop_loss |
| `invalidate` (long) | phase exhaustion/distribution после bounce |
| `invalidate` (long) | price < invalidation_below |
| `fix_profit_tp1/tp2` | price crosses TP |
| `stop_warning` | price within 0.2% of SL |
| `phase_change` | lifecycle phase changed (info) |

**Убрано:** `confirm_lost` при score 66 < 68 без движения цены.

---

## 10. Telegram & cooldowns

| Param | Value |
|-------|-------|
| Entry cooldown | 45 min / symbol:direction |
| Follow-up cooldown | 5 min / message_key |
| Preflight | 3 retries, soft-fail → watch без TG |

### Message types
- **Entry:** `DUMP SHORT` / `LONG` · CONFIRMED · score · confirm reasons · levels · main-bot blocked (advisory)
- **Follow-up:** `SIGNAL OFF` · reclaim / lifecycle / SL
- **TP:** fix profit TP1/TP2

---

## 11. Default pinned universe

| Symbol | Mode | Rationale |
|--------|------|-----------|
| JCTUSDT | short | memecoin pump fade |
| BEATUSDT | both | lifecycle flips post-dump |
| VELVETUSDT | long | post-dump bounce hunt |
| HYPEUSDT | long | bounce |
| BTCUSDT | both | context anchor |

Scanner может добавить до 12 dynamic symbols; pinned modes **не перезаписываются** scanner bias для DEFAULT_SYMBOLS.

---

## 12. Independent verification

`scripts/independent_batch.py` + `beat_check.py`:
- Raw REST klines, собственный RSI
- Scoring long vs short без hunt FSM
- Snapshot: `hunt/data/snapshots/hunt_independent_batch.json`

Использовать для **сверки** hunt TG vs sanity check (JCT invalid_short кейс).

---

## 13. Ops

```bash
# smoke hygiene (main repo)
python scripts/clean_session_data.py --mode smoke --config config.toml

# run watch
python hunt/scripts/watch.py --interval 60

# logs
tail -f hunt/data/dump_minute_watch.log

# grep blocks
grep watch_alert_blocked hunt/data/dump_minute_watch.log
```

### Health signals
- `watch_telegram_ready` — TG ok
- `watch_alert_blocked` — gate сработал (premature exhaustion)
- `watch_followup_sent` — structural invalidate / TP
- `hunt_scan_refresh` — scanner ok
- `data_missing` in watch_tick — REST partial fail

---

## 14. Backtest, replay & calibration (факт по коду)

Hunt **не имеет** полноценного event-driven backtest как `main.py backtest`. Есть **набор offline/live инструментов** — сверяй промпт аудита с этой таблицей.

### Что реализовано

| Инструмент | Файл | Что делает |
|------------|------|------------|
| **Tracker reconcile** | `scripts/reconcile_signals.py` | REST `5m` klines с `opened_at` → SL/TP state machine для **active**; `--backfill-legacy` для closed без reason |
| **Outcome backfill** | `param_calibration.backfill_legacy_outcomes` | То же через `calibrate_all` (5m window) |
| **Mini backtest helper** | `signal_audit.backtest_levels_on_bars` | SL/TP на списке `(high, low, close)` баров — **функция есть, в /signal audit пока не вызывается** |
| **Outcome calibration** | `scripts/calibrate_all.py` | universal + per-symbol gates из closed outcomes, tick JSONL, pump_history, REST 7d profile |
| **Level calibration** | `scripts/calibrate_levels.py` | SL caps из tracker outcomes |
| **Outcomes report** | `scripts/outcomes_report.py` | score buckets × win/loss |
| **Tick history sample** | `param_calibration.sample_tick_profiles` | tail `dump_minute_watch.jsonl` → median chg/sl_dist per symbol |
| **Pump backfill** | `pump_history.backfill_from_jsonl` | pump legs из tick JSONL |
| **Independent replay** | `critical_audit.py`, `signal_audit.audit_probe_row` | indie confirm/fuel vs bot на одном snapshot |
| **Supervised session** | `scripts/supervised_session.py` | N часов watch + `verify_diff` pause/resume |
| **Lab experiment** | `scripts/beat_dump_experiment.py` | per-symbol indicator matrix (BEAT-style) |

### Чего нет (gap)

- **JSONL replay MVP** — `hunt/scripts/jsonl_replay.py` + `hunt_watch/jsonl_replay.py`: tail `dump_minute_watch*.jsonl` → `confirm_dump/long` + `evaluate_alert_gate` + `confirm_min` sweep
- ~~**WS `@kline_5m` fast tick**~~ — реализовано: grace 2s + merge в REST `5m_closed`
- **Полный signal-path backtest** — нет forward PnL label на replay rows (join tracker + 5m bars — next)

### Рекомендуемый offline pipeline

```bash
.venv/bin/python hunt/scripts/outcomes_report.py
.venv/bin/python hunt/scripts/reconcile_signals.py --backfill-legacy
.venv/bin/python hunt/scripts/calibrate_all.py
.venv/bin/python hunt/scripts/calibrate_levels.py
.venv/bin/python hunt/scripts/reconcile_signals.py   # active reconcile
```

---

## 15. Binance public API (Hunt usage)

Только **публичные** USD-M endpoints. Источник: `watch._fetch_rest_pack`, `HuntWsFeed`, `param_calibration` direct klines.

### Используется сейчас

| Канал | Endpoints / streams | В hunt |
|-------|---------------------|--------|
| REST | `klines` 1m–1d | Primary frames (60s poll) |
| REST | `ticker/24h`, `exchangeInfo` | Scanner, universe |
| REST | `premiumIndex`, `fundingRate`, `fundingRate` history | Market layer |
| REST | `/futures/data/openInterest`, OI hist 5m | OI flush/build, z-score |
| REST | global/top/account L/S ratio + 5m series | Crowded longs/shorts |
| REST | taker long/short ratio 5m/15m/1h | Sell/buy pressure |
| REST | `basis`, `aggTrades`, `depth`, `bookTicker` | Microstructure |
| REST | Spot companion (engine) | lead-lag vs perp |
| WS | `wss://fstream.binance.com/market/stream` | Routed endpoint (legacy off 2026-04-23) |
| WS | `!forceOrder@arr` | Liquidation cascades 5m |
| WS | `!markPrice@arr@1s` | Live mark/index/funding + **`ap`** (basis gate) |
| WS | `@aggTrade` per symbol (cap 24) | Taker delta via **`nq`** (RPI excluded) |
| WS | `@kline_5m` per symbol | Fast confirm + 2s grace |

### Не используется (кандидаты на улучшение)

| Возможность | Зачем hunt |
|-------------|------------|
| WS `@kline_1m` | Sub-5m confirm (сейчас только 5m WS) |
| WS `@rpiDepth` | RPI volume excluded from `nq` — optional CVD supplement |
| WS `@depth` diff | Order book imbalance live (сейчас REST snapshot) |
| `indexPriceKlines` / `markPriceKlines` | Basis vs spot divergence |
| `longShortRatio` 1d history | Regime calibration |
| `topLongShortPositionRatio` vs `topLongShortAccountRatio` | Split whale vs retail crowding |
| Continuous `openInterest` WS | OI delta без REST budget hit |

Ограничение: `/futures/data/*` — **1000 req / 5 min / IP**; hunt batch OI на minute tick, не per-second.

---

## 16. Roadmap (не реализовано)

- VELVET pinned mode override vs scanner short bias
- HTF gate: require bear div OR dump_active for exhaustion short
- Cap universe: core 5 + top-3 scanner (latency)
- Follow-up TG telemetry dashboard
- Optional: WS 1m trigger вместо 60s REST poll

---

## 17. File map (data)

| Path | Content |
|------|---------|
| `hunt/data/hunt_watchlist.json` | Scanner output |
| `hunt/data/dump_minute_watch.jsonl` | Current-day ticks |
| `hunt/data/dump_minute_watch-YYYY-MM-DD.jsonl` | Rotated daily archive |
| `hunt/data/hunt_signal_state.json` | Tracker (active/closed) |
| `hunt/data/hunt_calibration.json` | Universal + per-symbol gates |
| `hunt/data/ewma_thresholds.json` | EWMA tick stats |
| `hunt/data/signal_events.jsonl` | Lifecycle event log |
| `hunt/data/signal_audit.jsonl` | /signal audit |
| `hunt/data/session/*.json` | Session hunt_high/low |
| `hunt/data/dump_watch_telegram_state.json` | TG cooldown |
| `hunt/data/snapshots/` | Independent batch |

Legacy `data/hunt_*` в корне — deprecated; migrate to `hunt/data/`.
