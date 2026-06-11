#!/usr/bin/env python3
"""Minute health monitor for Hunt Watch — process, ticks, TG, optional verify."""

from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "hunt"))

from hunt_watch.bootstrap import bootstrap

bootstrap()

from hunt_watch.paths import TICK_JSONL
from hunt_watch.targets import DEFAULT_SYMBOLS

LOG = logging.getLogger("hunt_minute_monitor")
OUT = ROOT / "logs" / "hunt_minute_monitor.jsonl"
CORE = tuple(DEFAULT_SYMBOLS)


def _proc_alive() -> tuple[bool, str]:
    r = subprocess.run(
        ["bash", str(ROOT / "scripts" / "hunt_supervised_session_mac.sh"), "--status"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    out = (r.stdout or "") + (r.stderr or "")
    if "running pid=" in out:
        return True, out.strip()
    return False, out.strip()


def _restart_supervised(hours: float) -> str:
    subprocess.run(
        ["bash", str(ROOT / "scripts/hunt_supervised_session_mac.sh"), "--stop"],
        cwd=ROOT,
        check=False,
    )
    time.sleep(2)
    r = subprocess.run(
        ["bash", str(ROOT / "scripts/hunt_supervised_session_mac.sh"), "--hours", str(hours)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    return (r.stdout or "").strip()


def _latest_ticks(symbols: tuple[str, ...]) -> dict[str, dict]:
    if not TICK_JSONL.exists():
        return {}
    want = {s.upper() for s in symbols}
    latest: dict[str, dict] = {}
    with TICK_JSONL.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            sym = str(row.get("symbol") or "").upper()
            if sym in want:
                latest[sym] = row
    return latest


def _log_tail_counts(since_marker: str | None) -> dict[str, int]:
    logs = sorted(ROOT.glob("logs/hunt_watch_*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not logs:
        return {"ticks": 0, "tg_sent": 0, "blocked": 0}
    text = logs[0].read_text(encoding="utf-8", errors="ignore")
    if since_marker:
        idx = text.find(since_marker)
        if idx >= 0:
            text = text[idx:]
    return {
        "ticks": text.count("watch_tick"),
        "tg_sent": text.count("watch_telegram_sent"),
        "blocked": text.count("watch_alert_blocked"),
        "ignition": text.count("hunt_ignition_telegram_sent"),
        "log": str(logs[0].name),
    }


def _summarize_row(sym: str, row: dict) -> dict:
    lc = row.get("lifecycle") or {}
    dump = row.get("dump") or {}
    long = row.get("long") or {}
    return {
        "symbol": sym,
        "ts": str(row.get("ts") or "")[:19],
        "price": row.get("price"),
        "phase": lc.get("phase"),
        "bias": lc.get("recommended_bias"),
        "short": dump.get("dump_score"),
        "short_ph": dump.get("phase"),
        "short_conf": dump.get("confirmed"),
        "long": long.get("long_score"),
        "long_ph": long.get("phase"),
        "long_conf": long.get("confirmed"),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Hunt watch minute monitor")
    parser.add_argument("--hours", type=float, default=6.0)
    parser.add_argument("--interval", type=int, default=60)
    parser.add_argument("--verify-every", type=int, default=10, help="Full verify_diff every N passes")
    args = parser.parse_args()

    OUT.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(ROOT / "logs/hunt_minute_monitor.log", encoding="utf-8"),
        ],
    )

    end_at = time.time() + args.hours * 3600.0
    session_marker: str | None = None
    pass_n = 0

    while time.time() < end_at:
        pass_n += 1
        alive, status = _proc_alive()
        restarted = False
        if not alive:
            LOG.warning("supervisor_down status=%s — restarting", status)
            restart_msg = _restart_supervised(args.hours)
            session_marker = datetime.now(UTC).strftime("%Y-%m-%d %H:%M")
            restarted = True
            LOG.info("supervisor_restarted %s", restart_msg)
            alive, status = _proc_alive()

        ticks = _latest_ticks(CORE)
        core_summary = [_summarize_row(s, ticks[s]) for s in CORE if s in ticks]
        log_counts = _log_tail_counts(session_marker)

        mismatch_count: int | None = None
        if pass_n % max(1, args.verify_every) == 0:
            try:
                from hunt_watch.monitor import run_verify_sync

                vr = run_verify_sync(limit=15)
                mismatch_count = int(vr["mismatch_count"])
                if mismatch_count > 0:
                    LOG.warning("verify_mismatches=%s alert=%s", mismatch_count, vr.get("alert_path"))
            except Exception as exc:
                LOG.exception("verify_failed")

        snap = {
            "ts": datetime.now(UTC).isoformat(),
            "pass": pass_n,
            "alive": alive,
            "status": status,
            "restarted": restarted,
            "log_counts": log_counts,
            "core": core_summary,
            "mismatches": mismatch_count,
        }
        with OUT.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(snap, default=str) + "\n")

        highlights = []
        for c in core_summary:
            if c.get("short_conf") or c.get("long_conf"):
                highlights.append(f"{c['symbol']} CONF")
            elif (c.get("short") or 0) >= 68 or (c.get("long") or 0) >= 68:
                highlights.append(f"{c['symbol']} imminent")
        LOG.info(
            "pass=%s alive=%s tg=%s blocked=%s ticks=%s highlights=%s mismatches=%s",
            pass_n,
            alive,
            log_counts.get("tg_sent"),
            log_counts.get("blocked"),
            log_counts.get("ticks"),
            highlights or "—",
            mismatch_count if mismatch_count is not None else "skip",
        )

        sleep_s = min(float(args.interval), max(1.0, end_at - time.time()))
        time.sleep(sleep_s)

    LOG.info("minute_monitor_done passes=%s", pass_n)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
