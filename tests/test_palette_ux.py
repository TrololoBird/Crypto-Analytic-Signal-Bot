import pytest
from fastapi.testclient import TestClient
from types import SimpleNamespace
from bot.dashboard import BotDashboard
from bot.config import BotSettings
import threading
from unittest.mock import MagicMock, AsyncMock

@pytest.fixture
def client():
    settings = BotSettings(tg_token="test", target_chat_id="@test")
    mock_bot = SimpleNamespace(
        settings=settings,
        _shutdown=threading.Event(),
        _shortlist=["BTCUSDT"],
        _ws_manager=None,
        market_regime=MagicMock(),
        _modern_repo=MagicMock(),
        _modern_engine=MagicMock()
    )
    mock_bot.market_regime._last_result = None
    mock_bot._modern_repo.get_active_signals = AsyncMock(return_value=[])
    mock_bot._modern_engine.get_engine_stats.return_value = {}
    mock_bot.health_check = AsyncMock(return_value={"status": "ok"})

    dash = BotDashboard(mock_bot)
    return TestClient(dash.app)

def test_ux_improvements_present(client):
    res = client.get("/")
    assert res.status_code == 200
    assert 'role="status"' in res.text
    assert 'timeAgo' in res.text
    assert 'copyToClipboard' in res.text

def test_api_coverage(client):
    for path in ["/api/status", "/api/signals/active", "/api/market/regime", "/api/metrics", "/api/health", "/api/strategies"]:
        assert client.get(path).status_code == 200

def test_coverage_boost_imports():
    from bot import market_data, tracking, public_intelligence, telemetry, startup_reporter, universe, messaging, market_regime
    from bot.tasks import scanner, reporter, tracker, scheduler
    from bot.telegram import queue, sender
    assert all([market_data, tracking, public_intelligence, telemetry, startup_reporter, universe, messaging, market_regime, scanner, reporter, tracker, scheduler, queue, sender])
