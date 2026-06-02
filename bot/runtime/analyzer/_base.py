"""Shared analyzer mixin state (typing anchor for mypy)."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bot.runtime.bot import SignalBot


class AnalyzerMixinBase:
    """Declares ``_bot`` for all analyzer mixins."""

    _bot: SignalBot
