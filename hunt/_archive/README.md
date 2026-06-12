# hunt/_archive — заархивированный код (Track 2 консолидация)

Сюда переносится код, который **не на live-пути** и не используется регрессией
(`verify_logic`). Архив, а не удаление: сохраняем как референс идей и историю.
Файлы здесь **не импортируются** активным деревом; их кросс-импорты могут быть
сломаны — это нормально, они не исполняются.

## 2026-06-12 — экспериментальный кластер `beat_*`

Перенесено (sprawl-консолидация, мастер-план v3 Track 2):

| Файл | Было | Почему |
|------|------|--------|
| `hunt_watch/beat_dump_lab.py` (1028 LOC) | tooling-only | per-symbol indicator-matrix лаба; не в live/verify |
| `hunt_watch/independent_short.py` (294) | tooling-only | импортировался только `beat_short_watch` |
| `scripts/beat_dump_experiment.py` | эксперимент | импортировал `beat_dump_lab` |
| `scripts/beat_short_watch.py` | эксперимент | импортировал `beat_dump_lab` + `independent_short` |
| (repo-root shim `scripts/beat_dump_experiment.py`) | удалён | цель заархивирована |

**Остаётся в активном дереве:** `hunt/scripts/beat_check.py` — несмотря на имя, это
verify-хелпер, его тянут `hunt_watch/monitor.py`, `scripts/verify_diff.py`,
`scripts/independent_batch.py`.

Проверено после переноса: `py_compile` (live-дерево) OK, `verify_logic` 120/120.
