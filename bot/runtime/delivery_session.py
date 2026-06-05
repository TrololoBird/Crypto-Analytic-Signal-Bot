"""Delivery session counters (isolated from alert modules to avoid import cycles)."""

from __future__ import annotations

from typing import Any


def delivery_session_snapshot(bot: Any) -> dict[str, int]:
    cap = int(getattr(bot.settings.delivery, "action_cap_per_session", 0) or 0)
    used = int(getattr(bot, "_session_action_delivered", 0) or 0)
    return {
        "session_action_delivered": used,
        "session_action_cap": cap,
        "session_action_remaining": max(0, cap - used) if cap > 0 else -1,
    }


__all__ = ["delivery_session_snapshot"]
