"""Early/ignition advisory — prep_shadow only, no TG by default."""

from __future__ import annotations

from typing import Any

ADVISORY_PHASES = frozenset({"impulse_initiating", "post_dump_bounce", "distribution"})


def is_advisory_phase(lifecycle: dict[str, Any] | None) -> bool:
    if not isinstance(lifecycle, dict):
        return False
    return str(lifecycle.get("phase") or "") in ADVISORY_PHASES
