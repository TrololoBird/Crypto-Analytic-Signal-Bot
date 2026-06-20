#!/usr/bin/env python3
"""8h autonomous Hunt loop: live watch cycles → telemetry rollup → static gates.

Agent-owned; operator only sets duration. Reports under hunt/data/session/autonomous_<run_id>/.

  .venv/bin/python scripts/hunt_autonomous_8h.py --hours 8 --cycle-minutes 45
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import subprocess
import sys
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HUNT = ROOT / "hunt"
DATA = HUNT / "data"
TICK_JSONL = DATA / "dump_minute_watch.jsonl"
LOG_DIR = ROOT / "logs"


def _utc_run_id() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def _log(msg: str, *, run_dir: Path) -> None:
    line = f"{datetime.now(UTC).isoformat()} | {msg}"
    print(line, flush=True)
    with (run_dir / "autonomous.log").open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def _external_watch_running() -> bool:
    proc = subprocess.run(
        ["pgrep", "-f", "[h]unt_core watch"],
        capture_output=True,
        text=True,
        check=False,
    )
    return bool(proc.stdout.strip())


def _watch_health_restart() -> dict | None:
    py = ROOT / ".venv/bin/python"
    proc = subprocess.run(
        [str(py), str(ROOT / "scripts/hunt_watch_health.py"), "--restart"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    line = (proc.stdout or proc.stderr or "").strip().splitlines()
    try:
        return {"exit": proc.returncode, "line": line[-1] if line else ""}
    except Exception:
        return {"exit": proc.returncode}


def _stop_watch() -> None:
    """Stop only hunt-owned watch (autonomous cycle). Skip if external live watch."""
    if os.environ.get("HUNT_AUTONOMOUS_KEEP_WATCH", "").strip().lower() in {"1", "true", "yes"}:
        return
    subprocess.run(["pkill", "-f", "[h]unt_core watch"], check=False)
    subprocess.run(["pkill", "-f", "hunt_core.* watch"], check=False)
    lock = DATA / "watch.pid"
    if lock.exists():
        lock.unlink(missing_ok=True)
    time.sleep(1.0)


def _run_static_gates() -> dict[str, str]:
    py = ROOT / ".venv/bin/python"
    cmds = [
        ("compileall", [str(py), "-m", "compileall", "-q", str(HUNT / "hunt_core")]),
        ("check_logic", [str(py), "-m", "hunt_core._dev.check_logic"], HUNT),
        ("check_scenarios", [str(py), "-m", "hunt_core._dev.check_scenarios"], HUNT),
        ("budget", [str(py), "-m", "hunt_core._dev.budget"], HUNT),
    ]
    out: dict[str, str] = {}
    for name, cmd, *rest in cmds:
        cwd = str(rest[0]) if rest else str(ROOT)
        proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, check=False)
        out[name] = "ok" if proc.returncode == 0 else f"fail:{proc.returncode}"
        if proc.returncode != 0:
            tail = (proc.stderr or proc.stdout or "")[-400:]
            out[f"{name}_detail"] = tail
    return out


def _analyze_ticks(*, since_bytes: int) -> dict:
    paths = Counter()
    lanes = Counter()
    confirmed = Counter()
    errors: list[dict] = []
    hot_carry = 0
    lines = 0
    if not TICK_JSONL.exists():
        return {"lines": 0, "error": "no_tick_jsonl"}
    with TICK_JSONL.open("rb") as fh:
        if since_bytes:
            fh.seek(since_bytes)
        for raw in fh:
            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            lines += 1
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            tp = str(row.get("tick_path") or "unknown")
            paths[tp] += 1
            if tp == "hot_carry":
                hot_carry += 1
            lane = row.get("delivery_lane")
            if lane:
                lanes[str(lane)] += 1
            dump = row.get("dump") or {}
            long_s = row.get("long") or {}
            if dump.get("confirmed"):
                confirmed["short"] += 1
            if long_s.get("confirmed"):
                confirmed["long"] += 1
            if row.get("error"):
                errors.append({"symbol": row.get("symbol"), "error": row.get("error")})
    return {
        "lines": lines,
        "tick_path": dict(paths),
        "delivery_lane": dict(lanes),
        "confirmed": dict(confirmed),
        "hot_carry": hot_carry,
        "row_errors": errors[:8],
    }


async def _run_watch_cycle(*, duration_s: float, interval: int) -> int:
    sys.path.insert(0, str(HUNT))
    from hunt_core.bootstrap import bootstrap

    bootstrap()
    from hunt_core.runtime.cycle import run_loop
    from hunt_core.runtime.state import request_stop

    def _on_sig(*_args: object) -> None:
        request_stop()

    signal.signal(signal.SIGINT, _on_sig)
    signal.signal(signal.SIGTERM, _on_sig)

    task = asyncio.create_task(
        run_loop((), interval, False, send_telegram=False),
        name="hunt_autonomous_watch",
    )
    try:
        await asyncio.wait_for(task, timeout=duration_s)
    except asyncio.TimeoutError:
        request_stop()
        try:
            await asyncio.wait_for(task, timeout=60.0)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
    except Exception as exc:
        request_stop()
        print(f"WATCH_ERROR {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        return 1
    return 0


def _boot_snapshot() -> dict | None:
    py = ROOT / ".venv/bin/python"
    proc = subprocess.run(
        [str(py), str(ROOT / "scripts/hunt_boot_snapshot.py")],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        return {"error": (proc.stderr or proc.stdout or "boot_snapshot_fail")[:300]}
    try:
        return json.loads(proc.stdout.strip())
    except json.JSONDecodeError:
        return {"error": "boot_snapshot_invalid_json"}


def _journal_add(run_id: str, cycle: int, report: dict) -> None:
    py = ROOT / ".venv/bin/python"
    tp = report.get("ticks", {}).get("tick_path", {})
    subprocess.run(
        [
            str(py),
            str(ROOT / "scripts/hunt_journal.py"),
            "add",
            "wave=auto8h",
            f"q_id=A8H-{run_id}-c{cycle}",
            "type=LIVE",
            "verdict=cycle_done",
            f"action={json.dumps({'run_id': run_id, 'cycle': cycle, 'tick_path': tp}, ensure_ascii=False)[:500]}",
        ],
        cwd=str(ROOT),
        check=False,
    )


def _improve_queue(run_dir: Path, cycle: int, report_path: Path) -> dict | None:
    py = ROOT / ".venv/bin/python"
    proc = subprocess.run(
        [
            str(py),
            str(ROOT / "scripts/hunt_agent_improve_cycle.py"),
            "--run-dir",
            str(run_dir),
            "--cycle-report",
            str(report_path),
            "--cycle",
            str(cycle),
        ],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        return {"error": (proc.stderr or proc.stdout or "improve_fail")[:300]}
    try:
        return json.loads(proc.stdout.strip())
    except json.JSONDecodeError:
        return {"error": "improve_invalid_json"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Hunt 8h autonomous live + gates loop")
    parser.add_argument("--hours", type=float, default=8.0)
    parser.add_argument("--cycle-minutes", type=int, default=45)
    parser.add_argument("--interval", type=int, default=30, help="Cold tick interval seconds")
    args = parser.parse_args(argv)

    run_id = _utc_run_id()
    run_dir = DATA / "session" / f"autonomous_{run_id}"
    run_dir.mkdir(parents=True, exist_ok=True)
    end_at = time.time() + args.hours * 3600.0
    cycle_s = max(300, args.cycle_minutes * 60)

    meta = {
        "run_id": run_id,
        "started_at": datetime.now(UTC).isoformat(),
        "hours": args.hours,
        "cycle_minutes": args.cycle_minutes,
        "run_dir": str(run_dir),
    }
    (run_dir / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    _log(f"AUTONOMOUS_START run_id={run_id} hours={args.hours} cycle_min={args.cycle_minutes}", run_dir=run_dir)

    cycle = 0
    cycles_summary: list[dict] = []

    while time.time() < end_at:
        cycle += 1
        remaining = end_at - time.time()
        this_cycle_s = min(float(cycle_s), remaining)
        if this_cycle_s < 120:
            break

        _log(f"CYCLE_{cycle}_START duration_s={this_cycle_s:.0f}", run_dir=run_dir)
        external_watch = _external_watch_running()
        tick_offset = TICK_JSONL.stat().st_size if TICK_JSONL.exists() else 0

        if external_watch:
            _log("CYCLE_SKIP_RUN external_watch=1 telemetry_and_gates_only", run_dir=run_dir)
            watch_ec = -1
        else:
            _stop_watch()
            watch_ec = asyncio.run(_run_watch_cycle(duration_s=this_cycle_s, interval=args.interval))
            _stop_watch()

        ticks = _analyze_ticks(since_bytes=tick_offset)
        gates = _run_static_gates()
        boot = _boot_snapshot()
        report = {
            "cycle": cycle,
            "at": datetime.now(UTC).isoformat(),
            "watch_exit": watch_ec,
            "duration_s": this_cycle_s,
            "ticks": ticks,
            "gates": gates,
            "boot": boot,
        }
        gate_fail = [k for k, v in gates.items() if v.startswith("fail")]
        if gate_fail:
            report["needs_fix"] = gate_fail
        path = run_dir / f"cycle_{cycle:02d}.json"
        path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
        improve = _improve_queue(run_dir, cycle, path)
        if improve:
            report["improve"] = improve
        cycles_summary.append(
            {
                "cycle": cycle,
                "lines": ticks.get("lines"),
                "hot_carry": ticks.get("hot_carry"),
                "tick_path": ticks.get("tick_path"),
                "gates_ok": not gate_fail,
                "improve_queued": (improve or {}).get("queued"),
            }
        )
        _journal_add(run_id, cycle, report)
        health = _watch_health_restart()
        if health:
            report["watch_health"] = health
        _log(
            f"CYCLE_{cycle}_DONE lines={ticks.get('lines')} hot_carry={ticks.get('hot_carry')} "
            f"gates={'ok' if not gate_fail else gate_fail} improve={(improve or {}).get('queued')}",
            run_dir=run_dir,
        )

        if time.time() >= end_at:
            break
        time.sleep(5.0)

    summary = {
        "run_id": run_id,
        "finished_at": datetime.now(UTC).isoformat(),
        "cycles": cycles_summary,
    }
    (run_dir / "summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    _log(f"AUTONOMOUS_DONE cycles={len(cycles_summary)} run_dir={run_dir}", run_dir=run_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
