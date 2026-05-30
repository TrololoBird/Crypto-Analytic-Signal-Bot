from __future__ import annotations

from types import SimpleNamespace

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from bot.dashboard import HAS_FASTAPI, BotDashboard


@pytest.mark.skipif(not HAS_FASTAPI, reason="fastapi not installed")
def test_dashboard_cors_and_security_headers() -> None:
    bot = SimpleNamespace(
        settings=SimpleNamespace(
            runtime=SimpleNamespace(
                dashboard_allow_origins=["http://127.0.0.1:8080", "http://localhost:8080"]
            )
        ),
        _bus=None,
        _shutdown=SimpleNamespace(is_set=lambda: False),
        _shortlist=[],
        _shortlist_source="test",
    )
    dashboard = BotDashboard(bot, port=8080, host="127.0.0.1")
    assert dashboard.app is not None

    client = TestClient(dashboard.app)
    response = client.get(
        "/api/status",
        headers={"Origin": "http://127.0.0.1:8080"},
    )

    assert response.status_code == 200
    assert response.headers.get("x-content-type-options") == "nosniff"
    assert response.headers.get("x-frame-options") == "DENY"
    assert response.headers.get("access-control-allow-origin") == "http://127.0.0.1:8080"
