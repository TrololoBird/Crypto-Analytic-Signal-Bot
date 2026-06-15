#!/usr/bin/env python3
"""Compact baseline snapshot for the Hunt autonomous loop (ФАЗА 0).

Один вызов → одна JSON-строка со срезом эмпирики: tracker WR/PnL, prep-shadow,
watch alive, свежие dump-JSONL. Цель — не печатать простыни в чат, а отдать
агенту компактные числа для baseline-записи в journal.

    .venv/bin/python scripts/hunt_boot_snapshot.py
"""
from __future__ import annotations

import json
import math
import subprocess
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "hunt" / "data"


class SnapshotError(RuntimeError):
    """Громкая ошибка снимка: файл состояния отсутствует/битый. Не глушится."""


def _load(name: str) -> dict:
    p = DATA / name
    if not p.exists():
        raise SnapshotError(f"нет файла состояния: {p}")
    try:
        return json.loads(p.read_text())
    except json.JSONDecodeError as exc:
        raise SnapshotError(f"битый JSON в {p}: {exc}") from exc


def tracker_summary() -> dict:
    st = _load("hunt_signal_state.json")
    sigs = st.get("signals")
    if not isinstance(sigs, dict):
        raise SnapshotError("hunt_signal_state.json: ключ 'signals' отсутствует или не dict")
    rows = list(sigs.values())
    closed = [s for s in rows if s.get("status") == "closed"]
    active = [s for s in rows if s.get("status") == "active"]
    wins, pnl = 0, 0.0
    for s in closed:
        # closed-сигнал ОБЯЗАН иметь конечный pnl_pct — иначе это битое состояние.
        p = s.get("pnl_pct")
        if p is None or not math.isfinite(float(p)):
            raise SnapshotError(f"closed сигнал без конечного pnl_pct: {s.get('symbol') or s}")
        p = float(p)
        pnl += p
        if p > 0.0:
            wins += 1
    n = len(closed)
    out = {"active": len(active), "closed": n, "wins": wins, "sum_pnl_pct": round(pnl, 2)}
    if n:
        out["wr_pct"] = round(wins / n * 100.0, 1)
    else:
        # WR над нулём сделок не определён — честно помечаем, не подставляем null/0.
        out["wr_pct_state"] = "undefined_zero_closed"
    return out


def prep_summary() -> dict:
    st = _load("prep_shadow_state.json")
    for key in ("active", "closed", "stats"):
        if key not in st:
            raise SnapshotError(f"prep_shadow_state.json: нет ключа '{key}'")
    return {
        "active": len(st["active"]),
        "closed": len(st["closed"]),
        "stats": st["stats"],
    }


def watch_alive() -> dict:
    out = subprocess.run(["pgrep", "-fl", "hunt_core.* watch"],
                         capture_output=True, text=True)
    mon = subprocess.run(["pgrep", "-fl", "hunt_agent_monitor_loop"],
                         capture_output=True, text=True)
    return {
        "watch": bool(out.stdout.strip()),
        "monitor": bool(mon.stdout.strip()),
    }


def latest_dumps() -> list[str]:
    files = sorted(DATA.glob("dump_minute_watch-*.jsonl"))
    if not files:
        raise SnapshotError("нет dump_minute_watch-*.jsonl — watch не пишет тики?")
    return [f.name for f in files[-2:]]


def _tail_jsonl_row(path: Path) -> dict | None:
    if not path.exists() or path.stat().st_size == 0:
        return None
    with path.open("rb") as fh:
        fh.seek(0, 2)
        pos = int(fh.tell())
        buf = b""
        while pos > 0:
            step = min(65_536, pos)
            pos -= step
            fh.seek(pos)
            buf = fh.read(step) + buf
            for line in reversed(buf.split(b"\n")):
                line = line.strip()
                if not line:
                    continue
                try:
                    return json.loads(line)
                except json.JSONDecodeError:
                    continue
    return None


def latest_tick_meta() -> dict:
    """Newest tick across daily archives + live staging buffer."""
    from hunt_core.paths import TICK_JSONL

    daily = sorted(DATA.glob("dump_minute_watch-*.jsonl"))
    paths = list(daily[-2:] if len(daily) >= 2 else daily)
    if TICK_JSONL.exists() and TICK_JSONL not in paths:
        paths.append(TICK_JSONL)
    best: dict | None = None
    best_ts = ""
    for path in paths:
        row = _tail_jsonl_row(path)
        if not row:
            continue
        ts = str(row.get("ts") or "")
        if ts >= best_ts:
            best_ts = ts
            best = row
    staging_lines = 0
    if TICK_JSONL.exists():
        staging_lines = sum(1 for ln in TICK_JSONL.read_text().splitlines() if ln.strip())
    if best is None:
        return {"staging_lines": staging_lines}
    lc = best.get("lifecycle") or {}
    return {
        "ts": best_ts,
        "symbol": best.get("symbol"),
        "phase": lc.get("phase"),
        "staging_lines": staging_lines,
    }


def main() -> int:
    snap = {
        "ts": datetime.now(UTC).isoformat(),
        "tracker": tracker_summary(),
        "prep_shadow": prep_summary(),
        "procs": watch_alive(),
        "latest_dump_jsonl": latest_dumps(),
        "latest_tick": latest_tick_meta(),
    }
    print(json.dumps(snap, allow_nan=False))
    return 0


if __name__ == "__main__":
    import sys
    try:
        raise SystemExit(main())
    except SnapshotError as exc:
        print(f"SNAPSHOT_FAIL: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
