"""Optional FastAPI dashboard (v9 package)."""

from __future__ import annotations

from .app import BotDashboard
from .live import DashboardLiveData
from .ws_broadcast import DashboardWSBroadcaster

__all__ = ["BotDashboard", "DashboardLiveData", "DashboardWSBroadcaster"]
