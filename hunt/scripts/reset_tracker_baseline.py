#!/usr/bin/env python3
"""Archive hunt tracker + TG cooldown and start a clean outcome baseline.

Keeps: dump_minute_watch.jsonl, pump_history, hunt_calibration, ewma, signal_events.
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

from hunt_watch.bootstrap import bootstrap

bootstrap()

from hunt_watch.paths import SESSION_DIR, SIGNAL_STATE, TELEGRAM_COOLDOWN


def _archive(path: Path, *, stamp: str) -> Path | None:
    if not path.is_file():
        return None
    dest_dir = SESSION_DIR / "archive"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{path.stem}_{stamp}{path.suffix}"
    dest.write_bytes(path.read_bytes())
    return dest


def reset_tracker_baseline(*, clear_tg_cooldown: bool = True) -> dict:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    archived: dict[str, str] = {}

    if SIGNAL_STATE.is_file():
        dest = _archive(SIGNAL_STATE, stamp=stamp)
        if dest:
            archived["tracker"] = str(dest)

    if clear_tg_cooldown and TELEGRAM_COOLDOWN.is_file():
        dest = _archive(TELEGRAM_COOLDOWN, stamp=stamp)
        if dest:
            archived["telegram_cooldown"] = str(dest)

    fresh = {
        "signals": {},
        "followup_sent": {},
        "baseline_reset_at": datetime.now(UTC).isoformat(),
        "baseline_note": "pre-P0 session archived; confirm_min=70 gates active",
    }
    SIGNAL_STATE.parent.mkdir(parents=True, exist_ok=True)
    SIGNAL_STATE.write_text(json.dumps(fresh, indent=2), encoding="utf-8")

    if clear_tg_cooldown:
        TELEGRAM_COOLDOWN.write_text("{}", encoding="utf-8")

    return {"ok": True, "archived": archived, "reset_at": fresh["baseline_reset_at"]}


def main() -> None:
    parser = argparse.ArgumentParser(description="Reset hunt tracker outcome baseline")
    parser.add_argument(
        "--keep-tg-cooldown",
        action="store_true",
        help="Keep dump_watch_telegram_state.json (funnel counts)",
    )
    args = parser.parse_args()
    rep = reset_tracker_baseline(clear_tg_cooldown=not args.keep_tg_cooldown)
    print(json.dumps(rep, indent=2))


if __name__ == "__main__":
    main()
