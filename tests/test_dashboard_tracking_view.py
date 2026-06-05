from __future__ import annotations

import pytest

from bot.dashboard.tracking_view import compute_progress, serialize_tracking_signal


def test_compute_progress_long_active_halfway_to_tp1() -> None:
    out = compute_progress(
        direction="long",
        status="active",
        entry=100.0,
        stop=95.0,
        tp1=110.0,
        tp2=120.0,
        tp3=130.0,
        current=105.0,
        tp1_hit_at=None,
        tp2_hit_at=None,
    )
    assert out["progress_pct"] == 50.0
    assert out["unrealized_pnl_pct"] == 5.0
    assert out["next_target_label"] == "Цель 1"


def test_compute_progress_short_pending() -> None:
    out = compute_progress(
        direction="short",
        status="pending",
        entry=50.0,
        stop=55.0,
        tp1=45.0,
        tp2=None,
        tp3=None,
        current=51.0,
        tp1_hit_at=None,
        tp2_hit_at=None,
    )
    assert "Лимит" in out["progress_label"]
    assert "до зоны" in out["progress_label"]
    assert out["progress_tone"] == "yellow"


def test_serialize_tracking_signal_uses_mark_price(monkeypatch: pytest.MonkeyPatch) -> None:
    class _WS:
        def get_mark_price_snapshot(self, symbol: str):
            return {"mark_price": 105.5}

    class _Bot:
        _ws_manager = _WS()

    row = {
        "symbol": "BTCUSDT",
        "setup_id": "ema_bounce",
        "direction": "long",
        "status": "active",
        "entry_mid": 100.0,
        "activation_price": 100.0,
        "stop_price": 95.0,
        "tp1_price": 110.0,
        "tracking_id": "t1",
    }
    payload = serialize_tracking_signal(row, _Bot())
    assert payload["current_price"] == 105.5
    assert payload["mark_price"] == 105.5
    assert payload["progress_pct"] == 55.0
