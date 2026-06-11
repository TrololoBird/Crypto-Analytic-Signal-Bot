# Hunt Research — 25 открытых вопросов

Документ для внешнего research-агента. Сформирован **2026-06-11** из:

| Источник | Что закрыто |
|----------|-------------|
| `report (1).md` (batch 1) | 65 вопросов A–J: **52 ✅**, **10 ⚠️**, **3 ❌** |
| `deep-research-report (25).md` | Группа **A (A1–A15)** — Binance WS/API, детально |
| `deep-research-report (24).md` | Executive summary (WS, `nq`/`ap`, TP partial, regime, CVD) — без per-question разбора |

**Группы B–J** в report (25) **не разобраны**; batch 1 дал эвристики, но не production-grade числа для Hunt.

Ниже — **25 приоритетных вопросов без надёжного ответа**, отсортированных по влиянию на код и калибровку `hunt/`.

---

## Как отвечать

Для каждого пункта нужно:

1. **Конкретное число / правило / ссылка** (official docs, paper, reproducible OSS, или live probe с методикой).
2. **Уровень уверенности**: official / research / community / Hunt-empirical-needed.
3. **Рекомендация для Hunt** (1–2 предложения): что менять в `param_store`, gates, tracker.

Не нужны общие обзоры MTF/WS — это уже закрыто в batch 1 и report (25).

---

## Critical — блокируют корректность данных и gates

### Q01 · B.16 — Задержка `kline` close на USDⓈ-M (официально + эмпирика)

**Вопрос:** Какова **распределённая** задержка между реальным close 5m/15m свечи и событием `x: true` на `wss://fstream.binance.com/market/stream` для USDⓈ-M? Нужны p50/p95/p99 (мс или сек), не только «~250 ms updates».

**Почему открыт:** Batch 1 — ⚠️ Partial; опора на Reddit/community. Report (25) не покрывал B.16. От этого зависит `grace_delay_seconds` в `hunt/hunt_watch/ws_feed.py`.

**Нужный формат ответа:** Таблица TF × перцентили; если official нет — методика live probe (N≥1000 closes, 10+ символов).

**Hunt:** `WS_KLINE_GRACE_SEC`, merge `5m_closed` vs intra-candle scoring.

---

### Q02 · A.8 — Точная формула поля `ap` (adjusted price) в mark price stream

**Вопрос:** Как **точно** считается `ap` в `@markPrice` после changelog 2026-03-16? MA window, источник цен, округление. Есть ли расхождение `ap` vs `(mark − index) / index` для basis-gate?

**Почему открыт:** В docs есть факт поля, **формула не воспроизведена**. Batch 1 — ✅ по факту наличия, не по math.

**Hunt:** `btc_alignment.py`, будущий basis gate; выбор `ap` vs raw mark/index.

---

### Q03 · D.35-nq — CVD / taker flow: `q` vs `nq` в `@aggTrade`

**Вопрос:** Для directional CVD и taker-buy-ratio на meme perps: использовать **`q`** (full) или **`nq`** (RPI excluded, с 2025-12-29)? Какой bias даёт каждый вариант на thin books?

**Почему открыт:** Report (24/25) упоминают `nq`, но **нет A/B** для signal gates. Batch 1 не различал.

**Hunt:** `signal_engine.py`, dump/pump confirm; WS migration на `/market/stream`.

---

### Q04 · I.60 — Политика для young listings (<50–100 баров 4h)

**Вопрос:** Best practice при пустом/коротком 4h: **(a)** synth 4h из 1h, **(b)** downgrade TF hierarchy, **(c)** exclude из universe, **(d)** proxy-only (funding/OI/basis)? Что делает industry при listing age <7d?

**Почему открыт:** Batch 1 — общий fallback list без валидации. В Hunt уже есть `frame_fallback.py` (synth 4h) — **нет evidence**, что это лучше exclude.

**Hunt:** `frame_fallback.py`, `prepare_frame` lite path; символы SOXL/SKHYNIX/BTW класс.

---

### Q05 · A.11 — Лимит ~10 входящих WS subscribe/msg в секунду (official?)

**Вопрос:** Подтверждает ли Binance лимит **~10 messages/sec** на входящие SUBSCRIBE/UNSUBSCRIBE? Sanctions при burst subscribe 50+ streams?

**Почему открыт:** Batch 1 — ⚠️ Partial (community only). Критично при 20–30 символах × multi-stream.

**Hunt:** `ws_feed.py` startup batching, reconnect storm.

---

## High — калибровка порогов и tracker

### Q06 · C.26 — VWAP + ATR: валидированные пороги для meme perps

**Вопрос:** Какие **численные** пороги distance-to-VWAP (в ×ATR или %) и ATR expansion считаются extreme для pump top / dump bottom на **low-cap perps** (не BTC/ETH)?

**Почему открыт:** Batch 1 — ⚠️ Partial, «no validated thresholds».

**Hunt:** exhaustion / re-entry gates в `signal_engine.py`, `level_calibration.py`.

---

### Q07 · C.28 — BTC correlation filter: soft/hard tiers

**Вопрос:** Для universe из 20–40 meme alts: оптимальные пороги **|ρ|** (rolling returns) для soft penalty vs hard block? Окно: 24h / 72h / 7d? Отдельно для «BTC-led» vs «idiosyncratic» names.

**Почему открыт:** Batch 1 — ⚠️ Partial (`|corr|>0.7` contra-BTC хуже — без soft tier). Hunt уже ставит **0.45 / 0.70** эмпирически — **не валидировано**.

**Hunt:** `btc_alignment.py` — `BTC_CORR_SOFT`, `BTC_CORR_HARD`.

---

### Q08 · D.32 — WR / payoff: short post-dump bounce vs active dump

**Вопрос:** Есть ли **эм pirical studies** или backtests: win rate и payoff short **bounce после −30/50% dump** vs short **active dump phase** vs **late bleed**? Разбивка по phase (accumulation / markup / distribution).

**Почему открыт:** Batch 1 — ❌ Not found. Ядро Hunt lifecycle (`lifecycle.py`, phase matrix).

**Hunt:** `phase_matrix_gate.py`, direction×phase auto-disable.

---

### Q09 · D.35 — Optimal window для aggTrade taker-buy-ratio

**Вопрос:** Lookback для taker-buy-ratio / CVD slope при confirm dump: **1m / 5m / 15m / 60m**? Нужен ответ с обоснованием volatility meme perps.

**Почему открыт:** Batch 1 — ❌ Not found.

**Hunt:** confirm stack в `signal_engine.py`, tie-in с Q03 (`nq`).

---

### Q10 · D.36 — Basis mark−index: extreme bps для meme perps

**Вопрос:** При каких **bps** basis (mark vs index) на meme perps считать overheated long / crowded short **на момент TG confirm**? Типичный range vs extreme (не только «50–200 bps typical»).

**Почему открыт:** Batch 1 — ⚠️ Partial.

**Hunt:** funding/basis confirm gate; связка с Q02 (`ap`).

---

### Q11 · D.34-cal — Порог liquidation cascade ($ notional)

**Вопрос:** Порог **forceOrder** notional (USD) за rolling window для «cascade confirm» на **alt perps** (не BTC): $1M / $5M / $10M? Window 1m vs 5m?

**Почему открыт:** Batch 1 описал каскады Oct 2025, **без порога для scanner gate**.

**Hunt:** `@forceOrder` на `/market/stream`, dump confirm.

---

### Q12 · E.39 — Partial TP: 50% vs 80% tiered by regime/phase

**Вопрос:** Когда **80% @ TP1** beats **50% @ TP1** на meme perps (hot pump / high score)? Есть ли payoff tables по phase или ADX regime?

**Почему открыт:** Batch 1 — ✅ для generic «50% standard», Hunt внедрил **50 normal / 80 hot** без external validation.

**Hunt:** `param_store.tracker_thresholds()`, `signal_tracker.py`.

---

### Q13 · E.40 — Bias-flip exit vs fixed SL: quantified tradeoff

**Вопрос:** При какой **choppiness** (ADX<20, ATR%, phase) bias-flip exit **улучшает** expectancy vs fixed ATR-SL? Hunt tracker считал profitable bias_flip как structural loss — нужен framework.

**Почему открыт:** Batch 1 — качественно «cuts winners in chop», без чисел.

**Hunt:** `signal_tracker.py`, `tracker_outcomes.py`, stats PnL vs structural WR.

---

### Q14 · E.41 — ATR multiplier SL для meme perps по phase

**Вопрос:** **2.0× vs 2.5× vs 3.0× ATR** SL: validated split для distribution phase vs bounce vs fresh markup? Wilder period 14 на 5m vs 15m?

**Почему открыт:** Batch 1 — диапазон 1.5–2.5× без phase split.

**Hunt:** TP/SL construction в tracker и alert payload.

---

### Q15 · F.49-cal — Phase×direction auto-disable: n и WR floor

**Вопрос:** Оптимальные **n_min** (10 vs 15 vs 20) и **WR_floor** (20% vs 25% vs 30%) для отключения ячейки phase×direction при small sample? Bayesian prior vs raw WR?

**Почему открыт:** Batch 1 — эвристика n<10–15. Hunt использует **n≥10, WR<25%** после forensic `/stats` — не валидировано.

**Hunt:** `phase_matrix_gate.py`.

---

### Q16 · F.45 — Walk-forward для Hunt signal frequency

**Вопрос:** IS/OOS split и **длина окна** при ~1–5 сделок/день, 20 символов, 5m/15m/1h MTF? 70/30 vs rolling 90d? Минимальный OOS n перед сменой `confirm_min`?

**Почему открыт:** Batch 1 — ⚠️ Partial (3–6mo IS heuristic).

**Hunt:** `autotune_runner.py`, `hunt_calibration.json`, post-baseline reset.

---

### Q17 · F.47-hunt — Стабильность confirm_min 70/72 при n<30

**Вопрос:** При n<30 closed outcomes: насколько безопасно держать **confirm_min_score=70** (calibration) vs regime default 60? Есть ли published guardrails для **score floor + ADX block** joint tuning?

**Почему открыт:** Hunt `/stats` показал 9/24 entries below 70 — misleading WR. Нужен external prior.

**Hunt:** `param_store.effective_hunt_params()`, `level_calibration.py`.

---

### Q18 · F.50 — Paper vs manual Telegram execution gap

**Вопрос:** Quantified gap **2025–2026** для crypto perp manual TG: latency чтения, slippage %, non-execution rate. Median vs p90 **worse-than-paper** в % expectancy.

**Почему открыт:** Batch 1 — ⚠️ Partial («10–30% worse» heuristic).

**Hunt:** `tg_backtest` в `stats_report.py`, forward horizon tuning (Q19).

---

### Q19 · G.54-hunt — tg_backtest forward_horizon_hours

**Вопрос:** Оптимальный **forward window** (часы) для оценки prep→confirm funnel: 4h / 8h / 24h? Как учитывать partial TP и bias_flip в label?

**Почему открыт:** Не в batch 65; Hunt показывает 9W/5L/12F — методология label не стандартизирована.

**Hunt:** `stats_report.py`, JSONL `signal_events`.

---

### Q20 · G.54-prep — Early/prep Telegram: signal-to-noise

**Вопрос:** Практики **prep alert** без auto-trading: какой % prep конвертируется в confirm на meme scanners? Рекомендуемый cooldown и max prep/hour?

**Почему открыт:** Batch 1 — ✅ conceptually two-tier; **нет metrics**. Hunt: `EARLY_TELEGRAM_ENABLED=False`.

**Hunt:** `early_alert.py`, funnel vs noise.

---

## Medium — инфраструктура, references, regime

### Q21 · A.12 — WS proxy / geo-block strategy (Binance-compliant)

**Вопрос:** Документированные или widely-audited паттерны **proxy failover** для public market data (не auth): health check, stream stickiness, rate-limit interaction.

**Почему открыт:** Batch 1 — ❌ Not found. Проект использует `discover_binance_proxies.py` — нужен external benchmark.

**Hunt:** `bot.market.proxy_bootstrap` pattern mirrored in hunt if needed.

---

### Q22 · B.17-cal — Grace delay: 1 vs 2 vs 3 секунды (empirical)

**Вопрос:** При multi-symbol watch (20+): какой **grace_delay_seconds** минимизирует false «unclosed» vs stale confirm? A/B на live USDⓈ-M.

**Почему открыт:** Batch 1 — ✅ «1–3s recommended» без калибровки. Зависит от Q01.

**Hunt:** `ws_feed.py`, `verify_logic.py` cases.

---

### Q23 · I.62 — Regime classifier beyond ADX для crypto futures

**Вопрос:** Production patterns **ensemble** (ADX + realized vol + choppiness / TTM squeeze) с **численными** порогами и validation 2024–2026? Не TradingView-only.

**Почему открыт:** Batch 1 — ⚠️ Partial; HMM/TTM в OSS bots не найдены.

**Hunt:** `market_regime.py` (bot) vs hunt regime display; ensemble roadmap из report (24).

---

### Q24 · H.58 — Open-source dump / liquidation scanner references

**Вопрос:** Активные **OSS** или academic repos (2024–2026): Binance perps liquidation aggregation, phase tagging, JSONL event export — architecture reference.

**Почему открыт:** Batch 1 — ❌ Not found.

**Hunt:** `jsonl_replay.py`, `beat_dump_lab.py`, event schema.

---

### Q25 · F.44-replay — JSONL replay MVP: threshold sweep design

**Вопрос:** Методология **offline replay** для rule-based crypto scanners: минимальный event schema, walk-forward на JSONL, anti-leakage для closed-bar, типичные pitfalls (lookahead, survivor bias).

**Почему открыт:** Не в batch 65; Hunt имеет заготовки `hunt/hunt_watch/jsonl_replay.py` без spec.

**Hunt:** `scripts/jsonl_replay.py`, calibration loop без live capital.

---

## Сводка по статусу исходных 65

| Категория | IDs из batch 1 | Включено в TOP-25 |
|-----------|----------------|-------------------|
| ❌ Not found | A.12, D.32, D.35, H.56, H.58 | Q08, Q09, Q21, Q24 (+ H.56 отложен — низкий приоритет для Hunt) |
| ⚠️ Partial | A.8, A.11, B.16, C.26, C.28, D.36, F.45, F.50, I.62 | Q01–Q07, Q10, Q16, Q18, Q23 |
| Hunt-specific (вне 65) | — | Q03, Q04, Q11–Q15, Q17, Q19, Q20, Q22, Q25 |

**Намеренно не включены** (уже закрыты report 25 + batch 1): legacy WS deadline, stream routing, 1024 limit, ping/pong, MTF merge anti-lookahead, Elder/TF hierarchy, ADX>25, funding>0.1%, OI+delta dump, partial TP 50% baseline, Telegram 1 msg/sec, Polars default, outdated tutorials.

---

## Рекомендуемый порядок для research-агента

**Волна 1 (5 вопросов):** Q01, Q02, Q03, Q06, Q08 — данные и core gates.

**Волна 2 (5):** Q07, Q09, Q10, Q12, Q15 — пороги corr/basis/TP/phase matrix.

**Волна 3 (5):** Q13, Q16, Q17, Q18, Q25 — tracker stats и calibration.

**Волна 4 (5):** Q04, Q05, Q11, Q22, Q23 — infra и listings.

**Волна 5 (5):** Q14, Q19, Q20, Q21, Q24 — UX, proxy, references.

---

## Связанные файлы в репозитории

| Файл | Роль |
|------|------|
| `docs/HUNT_IMPLEMENTER_PROMPT.md` | Контекст implementer |
| `docs/CRITICAL_AUDIT_PROMPT.md` | Audit checklist |
| `hunt/ARCHITECTURE.md` | Hunt architecture |
| `hunt/hunt_watch/param_store.py` | Effective thresholds |
| `hunt/hunt_watch/phase_matrix_gate.py` | Phase×direction gate |
| `hunt/hunt_watch/stats_report.py` | `/stats`, tg_backtest |
| `hunt/data/hunt_calibration.json` | confirm_min=70 baseline |

---

*Следующий шаг: передать research-агенту **одну волну (5 вопросов)** за запрос, с ссылкой на этот файл.*
