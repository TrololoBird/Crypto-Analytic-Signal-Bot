#!/usr/bin/env python3
"""Close tracker rows stuck active > N days (audit P0).

  cd hunt && ../.venv/bin/python -m hunt_core._dev.purge_stale_tracker
  cd hunt && ../.venv/bin/python -m hunt_core._dev.purge_stale_tracker --dry-run
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime, timedelta

from hunt_core.bootstrap import bootstrap

bootstrap()

from hunt_core.paths import SIGNAL_STATE
from hunt_core.track.tracker import (
    close_signal,
    load_tracker_state,
    save_tracker_state,
    _is_signal_active,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Purge stale active hunt tracker signals")
    parser.add_argument("--days", type=float, default=5.0, help="Close active older than N days")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    state = load_tracker_state(SIGNAL_STATE)
    signals = state.get("signals") or {}
    cutoff = datetime.now(UTC) - timedelta(days=args.days)
    closed: list[str] = []

    for key, sig in list(signals.items()):
        if not isinstance(sig, dict):
            continue
        if not _is_signal_active(sig):
            continue
        raw = sig.get("opened_at") or sig.get("registered_at")
        if not raw:
            continue
        try:
            opened = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            if opened.tzinfo is None:
                opened = opened.replace(tzinfo=UTC)
        except (TypeError, ValueError):
            continue
        if opened >= cutoff:
            continue
        sym = str(sig.get("symbol") or key.split(":")[0])
        direc = str(sig.get("direction") or key.split(":")[-1])
        closed.append(key)
        if args.dry_run:
            print(f"would_close {key} opened={raw[:19]}")
            continue
        close_signal(
            state,
            symbol=sym,
            direction=direc,
            reason="audit_stale_purge",
            now=datetime.now(UTC),
        )
        print(f"closed {key} opened={raw[:19]}")

    if not args.dry_run and closed:
        save_tracker_state(state, SIGNAL_STATE)
    print(f"done n={len(closed)} dry_run={args.dry_run}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
