"""Static operational dashboard UI loader."""

from __future__ import annotations

from pathlib import Path


_DASHBOARD_HTML = (Path(__file__).parent / "static" / "dashboard.html").read_text(
    encoding="utf-8"
)


def dashboard_html() -> str:
    """Return the dashboard HTML document."""
    return _DASHBOARD_HTML
