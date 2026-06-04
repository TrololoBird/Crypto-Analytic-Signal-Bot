# План улучшений v9 (~2 часа агента)

> Сроки в **часах агента**, не календарных недель.

## Статус (2026-06-04)

> Полный план выполненных/оставшихся задач (волны E1–F11): **[PROJECT_ROADMAP_AND_STATUS.md](PROJECT_ROADMAP_AND_STATUS.md)**.

## Статус (2026-06-02)

| Блок | Статус |
|---|---|
| A — Hygiene (дубликаты, deps, docs) | ✅ |
| B — Shortlist + delivery wiring | ✅ |
| C — Spot companion (реализация, не stub) | ✅ |
| Live smoke 16 мин | ⏳ по запросу |

---

## A — Hygiene ✅

- Удалены дубликаты `bot/features/{core,advanced,oscillators}.py`
- Hot path: `prepare_frame.py` (Wilder, BB ddof=1, без shift(-N))
- Удалён extra `[ml]` (pandas/lightgbm без импортов)
- Документация: `docs/features/INDICATORS.md`, `CONNECTOR_DECISION.md`, `DEPENDENCIES.md`
- `contracts.py`: runtime path `bot/runtime/bot.py`

---

## B — Shortlist + delivery ✅

- Phases 2–7 в коде: subscription planner, data readiness, medium refresh, unified routing, filter stages, watch screener
- `DELIVERY_SUCCESS_STATUSES = {sent, logged}` → `selected.jsonl` через `delivered`
- Composable filters: все 8 stages (`stop`, `rr`, `scoring`, `min_score` wired)
- `diagnostics/runtime/health.py`: WS check через `state_snapshot`
- `tests/test_merge.py`: fix `valid_until` для historical `created_at`

---

## C — Spot companion ✅

- `bot/market/spot_companion.py` — public spot REST (price + 1m klines)
- `bot/runtime/spot_refresh_runner.py` — periodic cache + enrichments
- Config: `[bot.spot_companion]` (`enabled=false` по умолчанию)
- Поля: `spot_lead_return_1m`, `spot_futures_spread_bps` в `PreparedSymbol` + contract

---

## Verify

```bash
source .venv/bin/activate
python scripts/clean_session_data.py --mode smoke --config config.toml   # before each live session
python -m compileall -q bot
python scripts/validate_config.py --config config.toml.example
python scripts/verify_refactor_gate.py
pytest tests/test_subscription_planner.py tests/test_engine_routing.py tests/test_merge.py tests/test_spot_companion.py -q
```

---

## Ops backlog (не блокер)

- Live smoke 16 мин + telemetry funnel report
- Калибровка 38 стратегий по hit-rate (REFACTOR_PLAN Phase 4)
