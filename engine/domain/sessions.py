"""DST-aware trading-session (ICT killzone) windows — single source of truth.

Both the ``session_killzone`` strategy and the confluence ``_session_killzone_score``
component consume this module so the two can never disagree on what counts as an
active killzone.

ICT killzones are defined in the *local* clock of their financial centre, not in
UTC. London and New York observe daylight saving, so their UTC offset shifts by
one hour in March and November. Anchoring each window to its IANA timezone makes
the UTC boundaries follow DST automatically instead of silently drifting half the
year (the previous hardcoded-UTC behaviour was correct only in winter).

Window definitions (local centre time) — chosen to reproduce the bot's historical
winter UTC windows exactly, while self-correcting in summer:

    London     07:00–10:00 Europe/London      (was 07:00–10:00 UTC, winter)
    NY         08:00–12:00 America/New_York    (was 13:00–17:00 UTC, winter)
    PreLondon  05:00–07:00 Europe/London       (was 05:00–07:00 UTC, winter)
    NYClose    15:00–17:00 America/New_York    (was 20:00–22:00 UTC, winter)
    Asia       00:00–03:00 UTC (fixed; Tokyo does not observe DST)
    Overlap    London ∩ NY  (both major sessions live)
"""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

_NY = ZoneInfo("America/New_York")
_LONDON = ZoneInfo("Europe/London")

# (start_hour, end_hour) in the centre's local clock; end is exclusive.
_LONDON_KZ = (7, 10)
_NY_KZ = (8, 12)
_PRE_LONDON_KZ = (5, 7)
_NY_CLOSE_KZ = (15, 17)
_ASIA_KZ_UTC = (0, 3)  # fixed UTC — JST has no DST

# Session names in priority order — must stay a subset of the consumers'
# _SESSION_QUALITY / scoring tables.
SESSION_NAMES: tuple[str, ...] = ("Overlap", "London", "NY", "Asia", "PreLondon", "NYClose")
MAJOR_SESSIONS: frozenset[str] = frozenset({"Overlap", "London", "NY"})


def _local_window_active(dt_utc: datetime, tz: ZoneInfo, start_h: int, end_h: int) -> bool:
    local_hour = dt_utc.astimezone(tz).hour
    if start_h == end_h:
        return False
    if start_h < end_h:
        return start_h <= local_hour < end_h
    return local_hour >= start_h or local_hour < end_h


def active_killzone(dt_utc: datetime | None = None) -> str | None:
    """Return the active ICT killzone name (DST-aware) or ``None`` outside all windows."""
    if dt_utc is None:
        dt_utc = datetime.now(UTC)
    elif dt_utc.tzinfo is None:
        dt_utc = dt_utc.replace(tzinfo=UTC)

    london = _local_window_active(dt_utc, _LONDON, *_LONDON_KZ)
    ny = _local_window_active(dt_utc, _NY, *_NY_KZ)
    if london and ny:
        return "Overlap"
    if london:
        return "London"
    if ny:
        return "NY"
    if _ASIA_KZ_UTC[0] <= dt_utc.astimezone(UTC).hour < _ASIA_KZ_UTC[1]:
        return "Asia"
    if _local_window_active(dt_utc, _LONDON, *_PRE_LONDON_KZ):
        return "PreLondon"
    if _local_window_active(dt_utc, _NY, *_NY_CLOSE_KZ):
        return "NYClose"
    return None


def in_killzone(dt_utc: datetime | None = None) -> bool:
    return active_killzone(dt_utc) is not None


def is_major_session(dt_utc: datetime | None = None) -> bool:
    """True only during London / NY / their overlap — the high-liquidity windows."""
    return active_killzone(dt_utc) in MAJOR_SESSIONS
