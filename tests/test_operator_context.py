"""Tests for operator market context formatters."""

from __future__ import annotations

from bot.dashboard.operator_context import (
    format_market_from_display_snapshot,
    format_runtime_ops_block,
)


def test_format_market_from_display_snapshot_rich_lines() -> None:
    text = format_market_from_display_snapshot(
        {
            "risk_label": "risk-off",
            "fear_greed_value": 59,
            "fear_greed_label": "Greed",
            "practical": "рынок больше поддерживает short",
            "breadth_positive": 34,
            "breadth_total": 51,
            "breadth_pct": 67.0,
            "tf_4h": "4h: легкий восходящий уклон",
            "tf_1h": "1h: легкий восходящий уклон",
            "tf_15m": "15m: легкий восходящий уклон",
            "btc_24h_pct": 0.3,
            "eth_24h_pct": 0.2,
            "sol_24h_pct": 0.5,
            "volume_btc_pct": 20.1,
            "volume_eth_pct": 14.7,
            "volume_sol_pct": 3.2,
            "volume_alts_pct": 62.0,
            "volume_stables_pct": 0.0,
            "macro_line": "PAXG +0.0% | mode risk-off",
            "corr_line": "corr 1h/7d proxy: ETH +0.35 flat",
            "corr_narrative": "corr: используется 24h co-direction proxy",
            "leaders": "PORTALUSDT +44.0%",
            "laggards": "NFPUSDT -37.7%",
            "tracking_active": 0,
            "tracking_pending": 0,
        }
    )
    assert "Контекст рынка" in text
    assert "fear/greed proxy" in text
    assert "Ширина рынка" in text
    assert "PORTALUSDT" in text
    assert "NFPUSDT" in text


def test_format_runtime_ops_block_includes_policy() -> None:
    text = format_runtime_ops_block(
        tag="startup",
        runtime_policy={
            "runtime_mode": "signal_only",
            "source_policy": "binance_only",
            "max_consecutive_stop_losses": 3,
            "stop_loss_pause_hours": 5,
        },
        readiness={"shortlist_source": "ws_light", "shortlist_size": 50},
        ws_health={"active_stream_count": 158, "reconnect_reason": "steady"},
        frame_readiness={"15m_ready_symbols": 4, "1h_ready_symbols": 4, "4h_ready_symbols": 0},
    )
    assert "Runtime" in text
    assert "signal_only" in text
    assert "ws_light" in text
    assert "158" in text
