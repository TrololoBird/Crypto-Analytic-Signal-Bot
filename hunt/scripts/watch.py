#!/usr/bin/env python3
"""Hunter watch CLI — thin entry to hunt_core runtime (H-B rewrite)."""

from __future__ import annotations

from hunt_core.bootstrap import bootstrap

bootstrap()

from hunt_core.runtime.bot import main

if __name__ == "__main__":
    main()
