# Hunt — 50 вопросов для доработки (web research)

Каждая **категория** (5 вопросов) подкреплена **≥5 источниками**. Ответы → действия в коде/ops.

---

## A. Binance USD-M REST & rate limits

**Источники:** [Binance OI Hist](https://developers.binance.com/docs/derivatives/usds-margined-futures/market-data/rest-api/Open-Interest-Statistics) · [Top LS Position](https://developers.binance.com/docs/derivatives/usds-margined-futures/market-data/rest-api/Top-Trader-Long-Short-Ratio) · [Funding Info](https://developers.binance.com/docs/derivatives/usds-margined-futures/market-data/rest-api/Get-Funding-Rate-Info) · [Change Log](https://developers.binance.com/docs/derivatives/change-log) · [VoiceOfChain Futures Guide](https://voiceofchain.com/academy/binance-api-documentation-futures)

| # | Вопрос | Ответ / действие |
|---|--------|------------------|
| A1 | Достаточно ли TTL кэша для `/futures/data/*` при лимите 1000/5min? | Да при stagger; мониторить `X-MBX-USED-WEIGHT-1M` — **done:** warning на fapi fail |
| A2 | Нужен ли отдельный stagger для OI vs LS vs taker? | Да — `rest_pack_specs` уже tiered; расширить jitter для scanner |
| A3 | Корректен ли `markets_by_id` для CCXT? | **fixed:** list-entry в `symbols.py` |
| A4 | Нужен ли `CMCCirculatingSupply` из changelog? | Опционально для OI/value ratio — backlog |
| A5 | Fallback при 429 на enrichment? | Exponential backoff + skip symbol tick, не нули — **done:** `safe_fetch` + readiness gate |

---

## B. CCXT Pro WebSocket

**Источники:** [CCXT Pro Manual](https://github.com/ccxt/ccxt/wiki/ccxt.pro.manual) · [CCXT gap-fill #26945](https://github.com/ccxt/ccxt/issues/26945) · [WS Gap Detection](https://voiceofchain.com/academy/websocket-gap-detection-crypto) · [Multi-socket #27452](https://github.com/ccxt/ccxt/issues/27452) · [Binance Mark Price WS](https://developers.binance.com/docs/derivatives/usds-margined-futures/websocket-market-streams/Mark-Price-Stream)

| # | Вопрос | Ответ / действие |
|---|--------|------------------|
| B1 | Backfill после WS 1006? | CCXT не делает — **done:** REST klines = source of truth; log `gap_fill=rest_on_tick` |
| B2 | Лимит подписок на одном сокете? | Использовать `streamLimits` / `subscriptionLimitByStream` — review при >24 symbols |
| B3 | Ping/pong интервал? | CCXT `keepAlive` default; Binance 60s без pong — monitor `ws_last_msg_age_s` |
| B4 | Order book gap после disconnect? | REST depth snapshot — уже `fetch_order_book_depth_snapshot` fallback |
| B5 | watchOHLCV vs REST для confirm? | Closed-bar confirm **только** REST prepared frames; WS = trigger only |

---

## C. Polars & финансовые null

**Источники:** [Polars missing data](https://docs.pola.rs/user-guide/expressions/missing-data/) · [Application Architect nulls](https://www.application-architect.com/posts/how-to-handle-null-values-in-polars/) · [fill_null API](https://docs.pola.rs/api/python/dev/reference/expressions/api/polars.Expr.fill_null.html) · [Resampling](https://docs.pola.rs/user-guide/transformations/time-series/resampling/) · [Polars book null](https://pola-rs.github.io/polars-book/user-guide/expressions/null/)

| # | Вопрос | Ответ / действие |
|---|--------|------------------|
| C1 | `fill_null(0)` в индикаторах допустим? | Только warmup; live gate — `finite_float_or_none` |
| C2 | NaN vs null в OHLCV? | `clean_non_finite` + отдельно `fill_nan` — audit `shared.py` |
| C3 | join_asof для OI series? | Helper `align.py` — wire в collect (backlog) |
| C4 | Lazy scan_parquet для replay? | Backlog E5 — снизит RAM на lake |
| C5 | corr() на коротких рядах? | Min 8 bars — уже в `_btc_beta_1h`; document |

---

## D. Mark / Index / Funding

**Источники:** [Mark Price Stream](https://developers.binance.com/docs/derivatives/usds-margined-futures/websocket-market-streams/Mark-Price-Stream) · [Funding quant guide](https://quantjourney.substack.com/p/funding-rates-in-crypto-the-hidden) · [Get Funding Info](https://developers.binance.com/docs/developers.binance.com/docs/derivatives/usds-margined-futures/market-data/rest-api/Get-Funding-Rate-Info) · [Binance Change Log](https://developers.binance.com/docs/derivatives/change-log) · [VoiceOfChain Futures](https://voiceofchain.com/academy/binance-api-documentation-futures)

| # | Вопрос | Ответ / действие |
|---|--------|------------------|
| D1 | Mark vs last для entry? | Mark для derivatives — **done:** premium_row без `or 0` |
| D2 | Basis% formula? | `(mark/index - 1) * 100` — collect + `mark_index_divergence` |
| D3 | Extreme funding contrarian? | Уже в dump_init_score + delivery regime |
| D4 | `fundingInfo` caps/floors? | Persist per symbol — enhance `fetch_funding_info_all` usage |
| D5 | Cross-exchange funding consensus? | **done:** flat fields `cross_funding_*` |

---

## E. OI / LS / Taker positioning

**Источники:** [OI Statistics](https://developers.binance.com/docs/derivatives/usds-margined-futures/market-data/rest-api/Open-Interest-Statistics) · [Top LS Position](https://developers.binance.com/docs/derivatives/usds-margined-futures/market-data/rest-api/Top-Trader-Long-Short-Ratio) · [binance-intelligence-mcp](https://github.com/mefai-dev/binance-intelligence-mcp) · [Taker ratio Blackperp](https://blackperp.com/academy/what-is-taker-buy-sell-ratio) · [arXiv P&D ML](https://arxiv.org/html/2412.18848v1)

| # | Вопрос | Ответ / действие |
|---|--------|------------------|
| E1 | top_account vs top_position? | Оба — account=ls_ratio, position=top_position_ls_ratio |
| E2 | Taker divergence от price? | Microstructure gate — `taker_ratio` veto paths |
| E3 | OI change 1h vs 5m? | Pack tier: full=1h, fast=5m OI delta |
| E4 | Composite smart-money score? | MTF + dump_init — optional MCP-style 6-factor |
| E5 | Required before analysis? | **done:** `REQUIRED_PREPARED_LIVE_FIELDS` + strict gate |

---

## F. Pump & dump detection

**Источники:** [arXiv 2412.18848](https://arxiv.org/html/2412.18848v1) · [Alpha Architect P&D](https://alphaarchitect.com/a-new-wolf-in-town-pump-and-dump-manipulation-in-cryptocurrency-markets/) · [Crime Science detector](https://link.springer.com/article/10.1186/s40163-018-0093-5) · [binance-intelligence-mcp](https://github.com/mefai-dev/binance-intelligence-mcp) · [Blackperp taker](https://blackperp.com/academy/what-is-taker-buy-sell-ratio)

| # | Вопрос | Ответ / действие |
|---|--------|------------------|
| F1 | Z-score volume anomaly? | Scanner radar — calibrate `anomaly_min_chg_24h_pct` |
| F2 | Co-occurrence price+volume? | Crime Science — align с `pump_history` |
| F3 | Illiquid coin focus? | Meme filter + liquidity_gate |
| F4 | Telegram pump groups? | Out of scope (no scraping); REST-only |
| F5 | Fuel score calibration? | `verify_logic` 125 cases + `calibrate_all.py` |

---

## G. Telegram delivery

**Источники:** [grammY flood](https://grammy.dev/advanced/flood) · [429 retry policies](https://telegramhpc.com/news/574) · [429/403 manual](https://fyw-telegram.com/blogs/1242) · [DEV crypto bots load](https://dev.to/mintscripts/why-most-telegram-crypto-bots-fail-under-load-and-how-to-fix-it-2193) · [python-telegram-bot wiki](https://github-wiki-see.page/m/python-telegram-bot/python-telegram-bot/wiki/Avoiding-flood-limits)

| # | Вопрос | Ответ / действие |
|---|--------|------------------|
| G1 | Per-chat queue? | Broadcaster buffers — add per-chat deque if burst alerts |
| G2 | Respect retry_after literally? | **done:** `_telegram_rate_limit_wait` |
| G3 | Split long reports? | **done:** `_split_telegram` 3900 chars |
| G4 | 403 blocked chat? | Dead-letter + remove chat_id — backlog |
| G5 | `--no-telegram` smoke default? | **done:** `live_smoke_5m.py` |

---

## H. Async runtime & monitoring

**Источники:** [Jeremy Knox watchdog](https://www.jeremyknox.ai/blog/live-trading-bot-hung-7-hours-built-horus/) · [structlog 2026](https://www.dash0.com/guides/python-logging-libraries) · [asyncio logging](https://www.zopatista.com/python/2019/05/11/asyncio-logging/) · [SuperFastPython queue](https://superfastpython.com/asyncio-log-blocking/) · [FastAPI async logging](https://medium.com/@dresraceran/implementing-async-logging-in-fastapi-middleware-b112aa9c0db8)

| # | Вопрос | Ответ / действие |
|---|--------|------------------|
| H1 | `asyncio.wait_for` на REST? | Apply to symbol tick — `SYMBOL_TICK_TIMEOUT_S` exists |
| H2 | Event loop watchdog? | Backlog — SIGTERM if tick stall >2× interval |
| H3 | QueueHandler logging? | structlog in deps — optional hot-path migration |
| H4 | log staleness external? | `live_smoke_5m` + `supervised_session` |
| H5 | Single instance lock? | **done:** `watch.pid` |

---

## I. Backtest / replay / labels

**Источники:** [HUNT_REWRITE_MIGRATION](HUNT_REWRITE_MIGRATION.md) · [lake sqlite](.) · [replay_parity_check.py](../scripts/replay_parity_check.py) · [gate_edge.py](../scripts/gate_edge.py) · [unified_labels](.)

| # | Вопрос | Ответ / действие |
|---|--------|------------------|
| I1 | jsonl vs lake parity? | Run `replay_parity_check.py` — expand window |
| I2 | unified_labels.jsonl truth? | 578 rows — prefer for calibration |
| I3 | gate_edge vs live confirm? | `gate_edge.py` measures confirmation edge |
| I4 | Synthetic 125 cases enough? | Extend with live symbol probes monthly |
| I5 | Walk-forward thresholds? | `param_store.walk_forward_thresholds` |

---

## J. Deploy & ops

**Источники:** [DEPLOY.md](DEPLOY.md) · [supervised_session.py](../scripts/supervised_session.py) · [health_rollup.py](../scripts/health_rollup.py) · [agent_monitor_once.py](../scripts/agent_monitor_once.py) · [SOLO_OPERATOR_PLAYBOOK](../../docs/SOLO_OPERATOR_PLAYBOOK.md)

| # | Вопрос | Ответ / действие |
|---|--------|------------------|
| J1 | One-command deploy? | **done:** `deploy_hunt.sh` |
| J2 | Full check CI? | **done:** `hunt_full_check.sh` |
| J3 | 5m smoke with anomaly scan? | **done:** `live_smoke_5m.py` |
| J4 | 6h supervised + verify_diff? | `supervised_session.py --hours 6` |
| J5 | Proxy discovery? | Repo `discover_binance_proxies.py` shared with main bot |

---

## Приоритетные правки (из research)

1. ✅ `finite_float_or_none` — no silent zeros  
2. ✅ `safe_fetch` logging  
3. ✅ WS gap documented + REST truth  
4. ✅ `markets_by_id` list fix  
5. ✅ Deploy/smoke tooling  
6. ⏳ Event loop watchdog (H2)  
7. ⏳ `join_asof` OI/funding (C3)  
8. ⏳ Per-chat TG queue (G1)  
