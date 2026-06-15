#!/usr/bin/env python3
"""Append-only journal helper for the Hunt autonomous loop.

Заменяет ручной разбор autonomous_journal.jsonl. Экономит токены: агент пишет
одной командой и читает компактную сводку вместо чтения всего файла.

Файл: hunt/data/session/autonomous_journal.jsonl

Записать вопрос/исход (поля передаются как key=value или --json '<obj>'):
    .venv/bin/python scripts/hunt_journal.py add \
        wave=2 q_id=Q38 type=GATE verdict=improved \
        question="confluence floor" action="alert_explain fuel 72->70"

Прочитать (что уже закрыто, чтобы не дублировать вопросы):
    .venv/bin/python scripts/hunt_journal.py asked            # список q_id + verdict
    .venv/bin/python scripts/hunt_journal.py summary          # счётчики verdict по волнам
    .venv/bin/python scripts/hunt_journal.py wave 2           # записи волны 2
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
JOURNAL = ROOT / "hunt" / "data" / "session" / "autonomous_journal.jsonl"


class JournalError(RuntimeError):
    """Громкая ошибка журнала: битая строка JSONL. Не глушится молча."""


def _read() -> list[dict]:
    if not JOURNAL.exists():
        return []
    rows = []
    for i, line in enumerate(JOURNAL.read_text().splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise JournalError(f"{JOURNAL}:{i} битая JSONL-строка: {exc}") from exc
    return rows


def _coerce(v: str):
    try:
        return json.loads(v)
    except json.JSONDecodeError:
        return v


def add(args: list[str]) -> None:
    rec: dict = {}
    for a in args:
        if a.startswith("--json"):
            continue
        if "=" in a:
            k, v = a.split("=", 1)
            rec[k] = _coerce(v)
    if "--json" in args:
        idx = args.index("--json")
        rec.update(json.loads(args[idx + 1]))
    rec.setdefault("ts", datetime.now(UTC).isoformat())
    JOURNAL.parent.mkdir(parents=True, exist_ok=True)
    with JOURNAL.open("a") as f:
        f.write(json.dumps(rec, default=str) + "\n")
    print(json.dumps({"appended": rec.get("q_id", "?"), "total": len(_read())}))


def asked() -> None:
    rows = _read()
    out = [
        {
            "q_id": r.get("q_id"),
            "wave": r.get("wave"),
            "type": r.get("type"),
            "verdict": r.get("verdict"),
        }
        for r in rows
        if r.get("q_id")
    ]
    print(json.dumps(out, default=str))


def summary() -> None:
    rows = _read()
    by_wave: dict = {}
    for r in rows:
        w = r.get("wave", "?")
        by_wave.setdefault(w, Counter())[r.get("verdict", "missing_verdict")] += 1
    print(json.dumps({str(k): dict(v) for k, v in sorted(by_wave.items(), key=str)}, default=str))


def wave(n: str) -> None:
    rows = [r for r in _read() if str(r.get("wave")) == str(n)]
    print(json.dumps(rows, default=str))


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    cmd = sys.argv[1]
    if cmd == "add":
        add(sys.argv[2:])
    elif cmd == "asked":
        asked()
    elif cmd == "summary":
        summary()
    elif cmd == "wave":
        wave(sys.argv[2] if len(sys.argv) > 2 else "1")
    else:
        print(json.dumps({"error": f"unknown cmd {cmd}"}))
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
