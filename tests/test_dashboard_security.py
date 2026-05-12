import pytest
from fastapi.testclient import TestClient
import json
from types import SimpleNamespace
from bot.dashboard import BotDashboard
from bot.domain.config import BotSettings, RuntimeConfig


def test_runtime_config_default_origins() -> None:
    config = RuntimeConfig()
    # Use sets for comparison to avoid ordering issues
    expected = {
        "http://127.0.0.1",
        "http://localhost",
        "http://127.0.0.1:8080",
        "http://localhost:8080",
    }
    assert set(config.dashboard_allow_origins) >= expected


def test_dashboard_security_headers() -> None:
    settings = BotSettings(tg_token="TOKEN_FOR_TESTING", target_chat_id="12345")
    bot_mock = SimpleNamespace(settings=settings)
    dashboard = BotDashboard(bot_mock)

    if not dashboard.app:
        pytest.skip("FastAPI not installed")

    client = TestClient(dashboard.app)
    response = client.get("/")

    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "DENY"
    assert response.headers["X-XSS-Protection"] == "1; mode=block"


def test_dashboard_cors_restricted_origins() -> None:
    settings = BotSettings(tg_token="TOKEN_FOR_TESTING", target_chat_id="12345")
    settings.runtime.dashboard_allow_origins = ["https://trusted.test"]
    bot_mock = SimpleNamespace(settings=settings)
    dashboard = BotDashboard(bot_mock)

    if not dashboard.app:
        pytest.skip("FastAPI not installed")

    client = TestClient(dashboard.app)

    # Trusted origin
    response = client.options(
        "/",
        headers={
            "Origin": "https://trusted.test",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert response.headers.get("Access-Control-Allow-Origin") == "https://trusted.test"

    # Untrusted origin
    response = client.options(
        "/",
        headers={
            "Origin": "https://malicious.test",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert response.headers.get("Access-Control-Allow-Origin") is None


def test_dashboard_initialization_without_settings() -> None:
    # Ensure it doesn't crash if bot has no settings (defensive check)
    bot_mock = SimpleNamespace()
    dashboard = BotDashboard(bot_mock)
    assert dashboard is not None


def test_dashboard_status_exposes_btc_bias(tmp_path) -> None:
    class MockRepo:
        async def get_tracking_stats(self) -> dict[str, int]:
            return {"active": 2}

        async def get_market_context(self) -> dict[str, str]:
            return {"btc_bias": "bearish"}

    settings = BotSettings(
        tg_token="TOKEN_FOR_TESTING",
        target_chat_id="12345",
        data_dir=tmp_path,
    )
    bot_mock = SimpleNamespace(
        settings=settings,
        _modern_repo=MockRepo(),
        _ws_manager=None,
        _shutdown=SimpleNamespace(is_set=lambda: False),
        _shortlist=[],
        market_regime=SimpleNamespace(_last_result=None),
    )
    dashboard = BotDashboard(bot_mock)
    if not dashboard.app:
        pytest.skip("FastAPI not installed")

    payload = TestClient(dashboard.app).get("/api/status").json()

    assert payload["open_signals"] == 2
    assert payload["btc_bias"] == "bearish"


def test_dashboard_html_hardens_formatters_hotkeys_and_refresh_loop() -> None:
    html = BotDashboard(SimpleNamespace())._get_html_dashboard()

    assert "Number.isFinite" in html
    assert "if (tabButton) tabButton.click();" in html
    assert "const activeButton = document.querySelector(\".nav-tab.active\")" in html
    assert "document.getElementById('btc-bias').textContent = data.btc_bias || '-'" in html
    assert "id=\"delivery-provider\"" in html
    assert "id=\"last-cycle-delivery\"" in html


def test_dashboard_recent_signals_prefers_selected_file(tmp_path) -> None:
    settings = BotSettings(
        tg_token="TOKEN_FOR_TESTING",
        target_chat_id="12345",
        data_dir=tmp_path,
    )
    analysis_dir = tmp_path / "telemetry" / "runs" / "run_1" / "analysis"
    analysis_dir.mkdir(parents=True)
    (analysis_dir / "candidates.jsonl").write_text(
        json.dumps(
            {
                "ts": "2026-05-12T00:01:00+00:00",
                "symbol": "BTCUSDT",
                "setup_id": "spread_strategy",
                "direction": "short",
                "score": 0.57,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (analysis_dir / "selected.jsonl").write_text(
        json.dumps(
            {
                "ts": "2026-05-12T00:02:00+00:00",
                "symbol": "ETHUSDT",
                "setup_id": "multi_tf_trend",
                "direction": "long",
                "score": 0.62,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    dashboard = BotDashboard(SimpleNamespace(settings=settings))

    rows = dashboard._get_recent_signals(limit=5)

    assert len(rows) == 1
    assert rows[0]["symbol"] == "ETHUSDT"
    assert rows[0]["source"] == "selected"
    assert rows[0]["delivery_status"] == "sent"


def test_dashboard_recent_signals_uses_delivery_attempts_before_candidates(
    tmp_path,
) -> None:
    settings = BotSettings(
        tg_token="TOKEN_FOR_TESTING",
        target_chat_id="12345",
        data_dir=tmp_path,
    )
    analysis_dir = tmp_path / "telemetry" / "runs" / "run_1" / "analysis"
    analysis_dir.mkdir(parents=True)
    (analysis_dir / "candidates.jsonl").write_text(
        json.dumps(
            {
                "ts": "2026-05-12T00:01:00+00:00",
                "symbol": "BTCUSDT",
                "setup_id": "spread_strategy",
                "direction": "short",
                "score": 0.57,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (analysis_dir / "delivery.jsonl").write_text(
        json.dumps(
            {
                "ts": "2026-05-12T00:02:00+00:00",
                "symbol": "BTCUSDT",
                "setup_id": "spread_strategy",
                "direction": "short",
                "score": 0.57,
                "delivery_status": "logged",
                "delivery_reason": "notifier_disabled",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    dashboard = BotDashboard(SimpleNamespace(settings=settings))

    rows = dashboard._get_recent_signals(limit=5)

    assert len(rows) == 1
    assert rows[0]["source"] == "delivery"
    assert rows[0]["delivery_status"] == "logged"
    assert rows[0]["delivery_reason"] == "notifier_disabled"


def test_dashboard_recent_signals_aggregates_candidates(tmp_path) -> None:
    settings = BotSettings(
        tg_token="TOKEN_FOR_TESTING",
        target_chat_id="12345",
        data_dir=tmp_path,
    )
    analysis_dir = tmp_path / "telemetry" / "runs" / "run_1" / "analysis"
    analysis_dir.mkdir(parents=True)
    rows = [
        {
            "ts": "2026-05-12T00:05:45+00:00",
            "symbol": "BTCUSDT",
            "setup_id": "spread_strategy",
            "direction": "short",
            "timeframe": "15m",
            "entry_reference_price": 100.12345,
            "score": 0.58,
        },
        {
            "ts": "2026-05-12T00:05:46+00:00",
            "symbol": "BTCUSDT",
            "setup_id": "depth_imbalance",
            "direction": "short",
            "timeframe": "15m",
            "entry_reference_price": 100.12341,
            "score": 0.57,
        },
        {
            "ts": "2026-05-12T00:05:47+00:00",
            "symbol": "ETHUSDT",
            "setup_id": "price_velocity",
            "direction": "long",
            "timeframe": "15m",
            "entry_reference_price": 200.0,
            "score": 0.60,
        },
    ]
    with (analysis_dir / "candidates.jsonl").open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row) + "\n")
    dashboard = BotDashboard(SimpleNamespace(settings=settings))

    recent = dashboard._get_recent_signals(limit=5)

    btc = next(item for item in recent if item["symbol"] == "BTCUSDT")
    assert btc["confluence_count"] == 2
    assert set(btc["confluence_setups"]) == {"spread_strategy", "depth_imbalance"}
    assert btc["delivery_status"] == "candidate"


@pytest.mark.asyncio
async def test_dashboard_api_endpoints_coverage() -> None:
    # Mock bot with enough attributes to satisfy the API handlers
    class MockRepo:
        async def get_active_signals(self, **kwargs: object) -> list[object]:
            return []

        async def get_market_context(self) -> dict[str, str]:
            return {"btc_bias": "neutral"}

    class MockWS:
        def get_stats(self) -> dict[str, int]:
            return {"avg_latency_overall_ms": 10}

        _symbols: list[str] = ["BTCUSDT"]

    settings = BotSettings(tg_token="TOKEN_FOR_TESTING", target_chat_id="12345")
    bot_mock = SimpleNamespace(
        settings=settings,
        _modern_repo=MockRepo(),
        _ws_manager=MockWS(),
        _shutdown=SimpleNamespace(is_set=lambda: False),
        _shortlist=[],
        market_regime=SimpleNamespace(_last_result=None),
        _modern_engine=SimpleNamespace(get_engine_stats=lambda: {}),
    )

    dashboard = BotDashboard(bot_mock)
    if not dashboard.app:
        pytest.skip("FastAPI not installed")

    client = TestClient(dashboard.app)

    # Test various API endpoints to increase coverage
    assert client.get("/api/status").status_code == 200
    assert client.get("/api/signals/active").status_code == 200
    # Returns error but 200 OK json
    assert client.get("/api/market/regime").status_code == 200
    assert client.get("/api/metrics").status_code == 200
    assert client.get("/api/strategies").status_code == 200

    # Test health endpoint
    async def mock_health() -> dict[str, str]:
        return {"status": "ok"}

    bot_mock.health_check = mock_health
    assert client.get("/api/health").status_code == 200

    # Test signals/recent endpoint (even if it returns empty)
    assert client.get("/api/signals/recent").status_code == 200

    # Test analytics/report
    class MockAnalytics:
        def __init__(self, **kwargs: object) -> None:
            pass

        async def generate_report(self, **kwargs: object) -> dict[str, object]:
            return {"summary": {}, "setup_reports": []}

    # Use a side-effect to mock the StrategyAnalytics class inside the dashboard module
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("bot.analytics.StrategyAnalytics", MockAnalytics)
        assert client.get("/api/analytics/report").status_code == 200

    assert client.get("/api/analytics/strategy-decisions").status_code == 200

    # Test root HTML
    response = client.get("/")
    assert response.status_code == 200
    assert "Signal Bot Dashboard" in response.text


def test_dashboard_strategy_decision_summary_reads_latest_telemetry(tmp_path) -> None:
    settings = BotSettings(
        tg_token="TOKEN_FOR_TESTING",
        target_chat_id="12345",
        data_dir=tmp_path,
    )
    analysis_dir = tmp_path / "telemetry" / "runs" / "run_1" / "analysis"
    analysis_dir.mkdir(parents=True)
    rows = [
        {
            "setup_id": "bb_squeeze",
            "status": "skip",
            "reason_code": "asset_fit.shortlist_not_routed",
        },
        {
            "setup_id": "bb_squeeze",
            "status": "signal",
            "reason_code": "pattern.raw_hit",
        },
        {
            "setup_id": "spread_strategy",
            "status": "reject",
            "reason_code": "filters.risk_reward_too_low",
        },
    ]
    with (analysis_dir / "strategy_decisions.jsonl").open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row) + "\n")

    dashboard = BotDashboard(SimpleNamespace(settings=settings))

    summary = dashboard._get_strategy_decision_summary(limit_files=1, max_rows=100)

    assert summary["total_rows"] == 3
    assert summary["status_counts"] == {"skip": 1, "signal": 1, "reject": 1}
    bb_row = summary["setups"]["bb_squeeze"]
    assert bb_row["total"] == 2
    assert bb_row["signal_rate"] == 0.5
    assert bb_row["top_reasons"][0]["reason"] == "asset_fit.shortlist_not_routed"
