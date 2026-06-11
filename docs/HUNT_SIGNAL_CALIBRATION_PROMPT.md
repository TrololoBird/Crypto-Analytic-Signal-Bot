# Hunt — Signal Calibration & Forensic Audit Prompt

**Скопируй блок «PROMPT» целиком в новый чат (Claude / Cursor Agent).**

Контекст кода: `hunt/ARCHITECTURE.md`, `hunt/docs/HUNT_CHANGELOG.md` (секция 2026-06-11).  
Операционный снимок: `.venv/bin/python scripts/hunt_boot_snapshot.py`

---

## PROMPT (copy from here)

```text
# HUNT SIGNAL CALIBRATION AUDIT — детальная проверка сигналов

Ты — lead engineer Hunt. Пользователь — architect / acceptance only.
Ты сам запускаешь все команды, читаешь данные, пишешь выводы и (если нужно) код.

## Цель аудита

Проверить, что система **улучшается, а не деградирует**:
1. Каждый доставленный TG-сигнал имеет обоснованный тезис (lifecycle, confirm, levels).
2. Tracker WR отражает **качество тезиса**, а не мелкие structural exits с микро-плюсом.
3. Replay-метрики совпадают с live (нет blind spots в JSONL, нет завышенного gate-pass).
4. Параметры калибруются **только по evidence** (outcomes, prep_shadow, replay), не «на глаз».

## Guardrails (non-negotiable)

- NO auto-trading. Public Binance only.
- Не снижать `confirm_min` / delivery fuel **72** пока `n_tracker_closed < 30`.
- prep_shadow WR <50% → delivery tighten держать; не ослаблять prep-shadow gate.
- Delivery invariant: validate → confluence → deliver — не обходить.
- Не коммить без явного запроса. Agent выполняет все команды сам.

---

## ФАЗА 0 — Baseline (один вызов)

```bash
.venv/bin/python scripts/hunt_boot_snapshot.py
PYTHONPATH=hunt .venv/bin/python hunt/scripts/outcomes_report.py
PYTHONPATH=hunt .venv/bin/python hunt/scripts/prep_shadow_report.py
PYTHONPATH=hunt .venv/bin/python hunt/scripts/verify_logic.py
PYTHONPATH=hunt .venv/bin/python hunt/scripts/verify_diff.py --limit 15
```

Зафиксируй: tracker WR/PnL/n, prep_shadow WR по tier, latest_tick.ts, verify_logic pass/total, verify_diff mismatches.

---

## ФАЗА 1 — Forensic по каждому closed tracker signal

Для каждого `status=closed` в `hunt/data/hunt_signal_state.json`:

| Поле | Вопрос |
|------|--------|
| symbol, direction, opened_at | Когда и что |
| entry_lifecycle_phase, entry_lifecycle_bias | Соответствует ли направлению? |
| score / fuel at entry | ≥72? |
| tp1, tp2, stop_loss, entry_lo/hi | Levels realistic? |
| extreme_hi, extreme_lo | MFE/MAE vs TP |
| close_reason, pnl_pct | thesis success или structural scratch? |
| close_lifecycle_phase | Фаза при выходе vs при входе |

**Классификация исхода (добавь колонку `thesis_outcome`):**
- `tp_hit` — TP1 или TP2
- `thesis_fail` — bias_flip / lifecycle_stale / bounce_invalidate **без** TP
- `stop_loss` — stop_hit
- `scratch_win` — structural exit с 0.15% < pnl < TP1 path (VELVET-type)
- `clean_win` — TP или structural exit с pnl ≥ half TP1 distance

Особое внимание:
- **dump_active short** при `entry_lifecycle_bias=wait` — должен ли был открыться?
- **exhaustion_at_high short** — WR и ADX/div gates
- **bias_flip** с положительным pnl но без TP — не считать «успехом» в калибровке

---

## ФАЗА 2 — Replay sweep (honest gate-pass)

```bash
PYTHONPATH=hunt .venv/bin/python -c "
from pathlib import Path
from hunt_watch.jsonl_replay import load_tick_rows, replay_row, resolve_tick_paths, block_reason_mix
from hunt_watch.paths import DATA
paths = resolve_tick_paths([DATA/'dump_minute_watch-2026-06-11.jsonl', DATA/'dump_minute_watch-2026-06-10.jsonl'])
rows = load_tick_rows(paths=paths, max_lines=25000)
cs=gs=lc=lg=0
for row in rows:
    rs=replay_row(row, direction='short')
    if rs.confirmed: cs+=1; gs+=int(rs.gate_ok)
    rl=replay_row(row, direction='long')
    if rl.confirmed: lc+=1; lg+=int(rl.gate_ok)
print('short gate', gs, '/', cs, round(100*gs/cs,1) if cs else 0)
print('long gate', lg, '/', lc)
print('short blocks', block_reason_mix(rows)['short'].most_common(8))
print('long blocks', block_reason_mix(rows)['long'].most_common(8))
"
```

Проверь:
- `resolve_tick_paths` включает staging `dump_minute_watch.jsonl`
- `not_anomaly` blocks на BTC/ETH — by design
- `filter_block` остаток — legitimate или false positive?
- Post-fix window (ts ≥ последний major deploy): confirms vs gate_ok

---

## ФАЗА 3 — Live vs indie (verify_diff)

```bash
PYTHONPATH=hunt .venv/bin/python hunt/scripts/verify_diff.py --limit 25
```

Для каждого mismatch:
- `bot_short_premature` — bot рано vs indie invalid_short: кто прав по последующему price action?
- `no_bot_tick` — universe gap
- confirm/phase/fuel drift — parity bug или ожидаемо?

---

## ФАЗА 4 — Prep-shadow calibration funnel

```bash
PYTHONPATH=hunt .venv/bin/python hunt/scripts/prep_shadow_report.py
```

- WR по tier (prep vs start)
- WR по phase (distribution, dump_active, exhaustion_at_high)
- confirm funnel % — сколько prep → live confirm
- Если start tier WR > prep tier → prep thresholds слишком мягкие
- Если overall WR <50% → **не** ослаблять delivery; усилить prep или delay TG

---

## ФАЗА 5 — Parameter decisions (evidence-only)

Перед любым изменением порога — таблица:

| Parameter | Current | Evidence | Proposed | Risk |
|-----------|---------|----------|----------|------|
| confirm_min | 72 | n_tracker, replay | hold/change | false positives |
| exhaustion_short_min_fuel | 78 | exhaustion WR | | |
| dump continuation min_rr | 1.10 | gate blocks | | |
| bias=wait short open | allowed? | VELVET fail | **block TG** | missed dumps |
| bias_flip → win if pnl>0.15% | yes | inflates WR | thesis_fail metric | reporting only |

**Приоритетные фиксы (если evidence подтверждает):**
1. Block delivery + tracker open when `direction=short` AND `recommended_bias=wait` AND NOT `_dump_continuation_short_ok` structural override.
2. `outcomes_report`: колонка `thesis_outcome`; WR «clean» vs «scratch».
3. Не трогать confirm_min до n≥30 closed с новыми gates.

---

## ФАЗА 6 — Verify после любых правок

```bash
PYTHONPATH=hunt .venv/bin/python hunt/scripts/verify_logic.py
python -m compileall -q hunt
# restart watch if hot-path changed
pgrep -fl hunt/scripts/watch
```

Обновить `hunt/docs/HUNT_CHANGELOG.md` одним блоком: что, почему, метрики до/после, что сознательно не трогали.

---

## Deliverables (в ответ пользователю)

1. **Таблица closed signals** с thesis_outcome (не только pnl).
2. **Replay gate summary** + top blockers.
3. **Список calibration changes** — только с evidence; отдельно «recommended» vs «applied».
4. **VELVET-class rule** — формулировка gate/tracker fix.
5. **Риски** — что ещё недостаточно данных (n<30, pump-phase long confirms, etc.).

Не генерируй 50 пунктов backlog. Только evidence-based actions.
```

---

## Связанные файлы

| Файл | Назначение |
|------|------------|
| `hunt/hunt_watch/alert_explain.py` | Delivery gates |
| `hunt/hunt_watch/signal_tracker.py` | Tracker open/close, bias_flip |
| `hunt/hunt_watch/tracker_outcomes.py` | WR classification |
| `hunt/hunt_watch/jsonl_replay.py` | Offline replay |
| `scripts/hunt_boot_snapshot.py` | Compact baseline |
| `scripts/hunt_journal.py` | Autonomous loop journal (local, gitignored data) |
| `hunt/docs/HUNT_CHANGELOG.md` | Human changelog |
