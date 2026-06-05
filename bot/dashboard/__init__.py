"""Optional FastAPI dashboard (v9 package).

Canonical entry: ``bot.dashboard.app.BotDashboard`` (mounted by ``SignalBot``).
``live.py`` / ``analytics.py`` / ``ws_broadcast.py`` are helpers - do not add
parallel FastAPI apps.
"""

from __future__ import annotations

from .app import BotDashboard
from .live import DashboardLiveData
from .ws_broadcast import DashboardWSBroadcaster

__all__ = ["BotDashboard", "DashboardLiveData", "DashboardWSBroadcaster"]
