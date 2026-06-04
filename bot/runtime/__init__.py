"""Bot runtime orchestration — SignalBot loop, handlers, delivery wiring."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .bot import SignalBot

__all__ = ["SignalBot"]


def __getattr__(name: str) -> object:
    if name == "SignalBot":
        from .bot import SignalBot

        return SignalBot
    msg = f"module {__name__!r} has no attribute {name!r}"
    raise AttributeError(msg)
